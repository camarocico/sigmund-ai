import logging
import json
import zlib
import uuid
import time
import re
from pathlib import Path
from cryptography.fernet import InvalidToken
from .model import model
from . import prompt, config, utils, attachments
from langchain.schema import HumanMessage, AIMessage, SystemMessage, \
    FunctionMessage
logger = logging.getLogger('heymans')


class Messages:
    
    def __init__(self, heymans, persistent=False):
        self._heymans = heymans
        self._persistent = persistent
        if self._persistent:
            self.load()
        else:
            self.init_conversation()
        
    def __len__(self):
        return len(self._message_history)
        
    def __iter__(self):
        for msg in self._message_history:
            yield msg
            
    def init_conversation(self):
        self._condensed_text = None
        metadata = self.metadata()
        metadata['answer_model'] = 'welcome-bot'
        self._conversation_title = config.default_conversation_title
        self._message_history = [['assistant', self.welcome_message(), 
                                  metadata]]
        self._condensed_message_history = [
            [role, content] for role, content, metadata
            in self._message_history[:]]        
            
    def metadata(self, message_id=None):
        return {'message_id': str(uuid.uuid4()) 
                    if message_id is None else message_id,
                'timestamp': utils.current_datetime(),
                'sources': self._heymans.documentation.to_json(),
                'search_model': self._heymans.model_config['search_model'],
                'condense_model': self._heymans.model_config['condense_model'],
                'answer_model': self._heymans.model_config['answer_model']}
        
    def append(self, role, message, message_id=None):
        metadata = self.metadata(message_id=message_id)
        self._message_history.append([role, message, metadata])
        self._condensed_message_history.append([role, message])
        self._condense_message_history()
        if self._persistent:
            self.save()
        return metadata
    
    def delete(self, message_id):
        message_to_remove = None
        condensed_message_to_remove = None
        for role, message, metadata in self._message_history:
            if metadata['message_id'] == message_id:
                message_to_remove = [role, message, metadata]
                condensed_message_to_remove = [role, message]
                break
        else:
            logger.info(f'message not found for deletion: {message_id}')
            return
        if message_to_remove:
            logger.info(f'deleting message: {message_id}')
            self._message_history.remove(message_to_remove)
        if condensed_message_to_remove:
            try:
                self._condensed_message_history.remove(
                    condensed_message_to_remove)
            except ValueError:
                logger.error(
                    f'Could not find condensed message to remove for {message_id}')
        if self._persistent:
            self.save()
            
    def prompt(self, system_prompt=None):
        """The prompt consists of the system prompt followed by a sequence of
        AI, user, and tool/ function messages.
        
        If no system prompt is provided, one is automatically constructed.
        Typically, an explicit system_prompt is provided during the search
        phase, but not during the answer phase.
        """
        if system_prompt is None:
            system_prompt = self._system_prompt()
        model_prompt = [SystemMessage(content=system_prompt)]
        msg_len = len(self._condensed_message_history)
        for msg_nr, (role, content) in enumerate(
                self._condensed_message_history):
            if role == 'assistant':
                model_prompt.append(AIMessage(content=content))
            elif role == 'user':
                model_prompt.append(HumanMessage(content=content))
            elif role == 'tool':
                if msg_nr + config.keep_tool_results < msg_len:
                    continue
                model_prompt.append(FunctionMessage(content=content,
                                                    name='tool_function'))
            else:
                raise ValueError(f'Invalid role: {role}')
        return model_prompt

    def welcome_message(self):
        if self._heymans.search_first:
            return config.welcome_message_with_search
        return config.welcome_message_without_search
        
    def _condense_message_history(self):
        system_prompt = self._system_prompt()
        messages = [{"role": "system", "content": system_prompt}]
        prompt_length = sum(len(content) for role, content
                            in self._condensed_message_history
                            if role != 'tool')
        logger.info(f'system prompt length: {len(system_prompt)}')
        logger.info(f'prompt length (without system prompt): {prompt_length}')
        if prompt_length <= config.max_prompt_length:
            logger.info('no need to condense')
            return
        condense_length = 0
        condense_messages = []
        while condense_length < config.condense_chunk_length:
            role, content = self._condensed_message_history.pop(0)
            condense_length += len(content)
            condense_messages.insert(0, (role, content))
        logger.info(f'condensing {len(condense_messages)} messages')
        condense_prompt = prompt.render(
            prompt.CONDENSE_HISTORY,
            history=''.join(
                f'You said: {content}' if role == 'system' 
                else f'User said: {content}'
                for role, content in condense_messages))
        self._condensed_text = self._heymans.condense_model.predict(
            condense_prompt)
        
    def _system_prompt(self):
        """The system prompt that is used for question answering consists of
        several fragments.
        """
        # There is always and identity and a list of attached files
        if self._heymans.search_first:
            system_prompt = [prompt.SYSTEM_PROMPT_IDENTITY_WITH_SEARCH]
        else:
            system_prompt = [prompt.SYSTEM_PROMPT_IDENTITY_WITHOUT_SEARCH]
        system_prompt.append(
            attachments.attachments_prompt(self._heymans.database))
        # For models that support this, there is also an instruction indicating
        # that a special marker can be sent to indicate that the response isn't
        # done yet. Not all models support this to avoid infinite loops.
        if self._heymans.answer_model.supports_not_done_yet:
            system_prompt.append(prompt.SYSTEM_PROMPT_NOT_DONE_YET)
        # If available, documentation is also included in the prompt
        if len(self._heymans.documentation):
            system_prompt.append(self._heymans.documentation.prompt())
        system_prompt.append(
            attachments.attachments_prompt(self._heymans.database))
        # And finally, if the message history has been condensed, this is also
        # included.
        if self._condensed_text:
            logger.info('appending condensed text to system prompt')
            system_prompt.append(prompt.render(
                prompt.SYSTEM_PROMPT_CONDENSED,
                summary=self._condensed_text))
        # Combine all non-empty prompt chunks
        return '\n\n'.join(chunk for chunk in system_prompt if chunk.strip())
        
    def _update_title(self):
        """The conversation title is updated when there are at least two 
        messages, excluding the system prompt and AI welcome message. Based on
        the last messages, a summary title is then created.
        """
        if len(self) <= 2 or \
                self._conversation_title != config.default_conversation_title:
            return
        title_prompt = [SystemMessage(content=prompt.TITLE_PROMPT)]
        title_prompt += self.prompt()[2:]
        self._conversation_title = self._heymans.condense_model.predict(
            title_prompt).strip('"\'')
        if len(self._conversation_title) > 100:
            self._conversation_title = self._conversation_title[:100] + '…'

    def load(self):
        try:
            conversation = self._heymans.database.get_active_conversation()
        except InvalidToken as e:
            self.init_conversation()
            self.save()
            logger.error(f'failed to get active conversation: {e}')
            return
        if not conversation['message_history']:
            self.init_conversation()
            return
        modified = False
        for _, _, metadata in conversation['message_history']:
            if 'message_id' not in metadata:
                metadata['message_id'] = str(uuid.uuid4())
                modified = True
        self._conversation_title = conversation['title']
        self._message_history = conversation['message_history']
        self._condensed_text = conversation['condensed_text']
        self._condensed_message_history = \
            conversation['condensed_message_history']
        if self._persistent and modified:
            self.save()
    
    def save(self):
        self._update_title()
        conversation = {
            'condensed_text': self._condensed_text,
            'message_history': self._message_history,
            'condensed_message_history': self._condensed_message_history,
            'title': self._conversation_title,
        }
        self._heymans.database.update_active_conversation(conversation)
