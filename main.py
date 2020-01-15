# -*- coding: utf-8 -*-
# main.py
# Copyright (C) 2018-2019 KunoiSayami
#
# This module is part of Things-Forward-telegram and is released under
# the AGPL v3 License: https://www.gnu.org/licenses/agpl-3.0.txt
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.
import re, time
from queue import Queue
from configparser import ConfigParser
from threading import Thread, Lock, Timer
import logging
from pymysql.err import ProgrammingError
import redis
from pyrogram import Client, Filters, api, Message, MessageHandler, \
	ContinuePropagation
import pyrogram.errors
from utils import get_forward_id, get_msg_from, is_bot, log_struct, forward_request
from fileid_checker import checkfile
from configure import configure

logger = logging.getLogger('forward_main')

class forward_thread(Thread):
	class id_obj(object):
		def __init__(self, _id: int):
			self.id = _id
	class build_msg(object):
		def __init__(self, chat_id: int, msg_id: int, from_user_id: int = -1, forward_from_id = -1):
			self.chat = forward_thread.id_obj(chat_id)
			self.message_id = msg_id
			self.from_user = forward_thread.id_obj(from_user_id)
			self.forward_from = forward_thread.id_obj(forward_from_id)
	queue = Queue()
	switch = True
	'''
		Queue tuple structure:
		(target_id: int, chat_id: int, msg_id: int|tuple, Log_info: tuple)
		`target_id` : Forward to where
		`chat_id` : Forward from
		`msg_id` : Forward from message id
		`Loginfo` structure: (need_log: bool, log_msg: str, args: tulpe)
	'''
	def __init__(self, client: Client):
		super().__init__(daemon=True)
		self.client = client
		#self.cut_switch = config.has_option('forward', 'cut_long_text') and config['forward']['cut_long_text'] == 'True'
		self.checker = checkfile.get_instance()
		self.configure = configure.get_instance()
		self.logger = logging.getLogger('fwd_thread')
		log_file_header = logging.FileHandler('log.log')
		log_file_header.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
		self.logger.addHandler(log_file_header)
	@staticmethod
	def put_blacklist(from_chat: int, from_id: int, log_control: log_struct = (False,), msg_raw: Message or None = None):
		#forward_thread.put(int(config['forward']['to_blacklist']), from_chat, from_id, log_control, msg_raw)
		forward_thread.put(configure.get_instance().blacklist, from_chat, from_id, log_control, msg_raw)
	@staticmethod
	def put(forward_to: int, from_chat: int, from_id: int, log_control: log_struct = (False,), msg_raw: Message or None = None):
		forward_thread.queue.put_nowait((forward_to, from_chat, from_id, log_control, msg_raw))
	@staticmethod
	def get() -> tuple:
		return forward_thread.queue.get()
	@staticmethod
	def get_status() -> bool:
		return forward_thread.switch
	def run(self):
		while self.get_status():
			target_id, chat_id, msg_id, Loginfo, msg_raw = self.get()
			try:
				r = self.client.forward_messages(target_id, chat_id, msg_id, True)
				if msg_raw is None:
					msg_raw = self.build_msg(chat_id, msg_id)
				self.checker.insert_log(r.chat.id, r.message_id, msg_raw.chat.id,
					msg_raw.message_id, get_msg_from(msg_raw), get_forward_id(msg_raw, -1))
				if Loginfo[0]:
					self.logger.info(Loginfo[1], *Loginfo[2:])
			except ProgrammingError:
				logger.exception("Got programming error in forward thread")
			except pyrogram.errors.exceptions.bad_request_400.MessageIdInvalid:
				pass
			except:
				print(target_id, chat_id, msg_id, None if msg_raw is None else 'NOT_NONE' , Loginfo)
				if msg_raw is not None and target_id != self.configure.blacklist:
					print(msg_raw)
				#self.put(target_id, chat_id, msg_id, Loginfo, msg_raw)
				logger.exception('Got other exceptions in forward thread')
			time.sleep(0.5)

class set_status_thread(Thread):
	def __init__(self, client: Client, chat_id: int):
		Thread.__init__(self, daemon=True)
		self.switch = True
		self.client = client
		self.chat_id = chat_id
		self.start()
	def setOff(self):
		self.switch = False
	def run(self):
		while self.switch:
			self.client.send_chat_action(self.chat_id, 'TYPING')
			# After 5 seconds, chat action will canceled automatically
			time.sleep(4.5)
		self.client.send_chat_action(self.chat_id, 'CANCEL')

class get_history_process(Thread):
	def __init__(self, client: Client, chat_id: int, target_id:  int or str, offset_id: int = 0, dirty_run: bool = False):
		Thread.__init__(self, True)
		self.checker = checkfile.get_instance()
		self.configure = configure.get_instance()
		self.client = client
		self.target_id = int(target_id)
		self.offset_id = offset_id
		self.chat_id = chat_id
		self.dirty_run = dirty_run
		self.start()
	def run(self):
		checkfunc = self.checker.checkFile if not self.dirty_run else self.checker.checkFile_dirty
		photos, videos, docs = [], [], []
		msg_group = self.client.get_history(self.target_id, offset_id=self.offset_id)
		self.client.send_message(self.chat_id, 'Now process query {}, total {} messages{}'.format(self.target_id, msg_group.messages[0]['message_id'],
			' (Dirty mode)' if self.dirty_run else ''))
		status_thread = set_status_thread(self.client, self.chat_id)
		self.offset_id = msg_group.messages[0]['message_id']
		while self.offset_id > 1:
			for x in list(msg_group.messages):
				if x.photo:
					if not checkfunc((x.photo.sizes[-1].file_id,)): continue
					photos.append((is_bot(x), {'chat':{'id': self.target_id}, 'message_id': x['message_id']}))
				elif x.video:
					if not checkfunc((x.video.file_id,)): continue
					videos.append((is_bot(x), {'chat':{'id': self.target_id}, 'message_id': x['message_id']}))
				elif x.document:
					if '/' in x.document.mime_type and x.document.mime_type.split('/')[0] in ('image', 'video') and not checkfunc((x.document.file_id)):
						continue
					docs.append((is_bot(x), {'chat':{'id': self.target_id}, 'message_id': x['message_id']}))
			try:
				self.offset_id = msg_group.messages[-1]['message_id'] - 1
			except IndexError:
				logger.info('Query channel end by message_id %d', self.offset_id + 1)
				break
			try:
				msg_group = self.client.get_history(self.target_id, offset_id=self.offset_id)
			except pyrogram.errors.FloodWait as e:
				logger.warning('Got flood wait, sleep %d seconds', e.x)
				time.sleep(e.x)
		if not self.dirty_run:
			self.client.send_message(self.configure.query_photo, 'Begin {} forward'.format(self.target_id))
			self.client.send_message(self.configure.query_video, 'Begin {} forward'.format(self.target_id))
			self.client.send_message(self.configure.query_doc, 'Begin {} forward'.format(self.target_id))
			for x in reversed(photos):
				forward_thread.put(self.configure.query_photo if not x[0] else self.configure.bot_for, self.target_id, x[1]['message_id'], msg_raw=x[1])
			for x in reversed(videos):
				forward_thread.put(self.configure.query_video if not x[0] else self.configure.bot_for, self.target_id, x[1]['message_id'], msg_raw=x[1])
			for x in reversed(docs):
				forward_thread.put(self.configure.query_doc if not x[0] else self.configure.bot_for, self.target_id, x[1]['message_id'], msg_raw=x[1])
		status_thread.setOff()
		self.client.send_message(self.chat_id, 'Query completed {} photos, {} videos, {} docs{}'.format(len(photos), len(videos), len(docs), ' (Dirty mode)' if self.dirty_run else ''))
		logger.info('Query %d completed%s, total %d photos, %d videos, %d documents.', self.target_id, ' (Dirty run)' if self.dirty_run else '', len(photos), len(videos), len(docs))
		del photos, videos, docs


def build_log(chat_id: int, message_id: int, from_user_id: int, froward_from_id: int):
	return {'chat': {'id': chat_id}, 'message_id': message_id, 'from_user':{'id': from_user_id},
		'forward_from_chat': {'id': froward_from_id}}

class bot_controler:
	def __init__(self):
		config = ConfigParser()
		config.read('config.ini')
		self.configure = configure.init_instance(config)
		self.redis = redis.Redis()
		self.app = Client(
			'inforward',
			config.get('account', 'api_id'),
			config.get('account', 'api_hash')
		)
		self.checker = checkfile.init_instance(config.get('mysql', 'host'), config.get('mysql', 'username'), config.get('mysql', 'password'), config.get('mysql', 'database'))
		self.redis.sadd('for_bypass', *list(map(int, config.get('forward', 'bypass_list')[1:-1].split(','))))
		self.redis.sadd('for_blacklist', *list(map(int, config.get('forward', 'black_list')[1:-1].split(','))))
		#self.bypass_list = [int(x) for x in eval(self.config['forward']['bypass_list'])]
		#self.black_list = [int(x) for x in eval(self.config['forward']['black_list'])]
		self.redis.mset(dict(map(lambda x: config.get('forward', 'special').split(': '), config.get('forward', 'special')[1:-1].replace('\'', '').split(', '))))
		#self.do_spec_forward = eval(config['forward']['special'])
		self.echo_switch = False
		self.detail_msg_switch = False
		#black_list_listen_mode = False # Deprecated
		self.delete_blocked_message_after_blacklist = False
		#self.authorized_users = eval(self.config['account']['auth_users'])
		self.redis.sadd('for_admin', *list(map(int, config.get('forward', 'auth_users')[1:-1].split(','))))
		self.redis.sadd('for_admin', config.getint('account', 'owner'))
		self.func_blacklist = None
		if self.configure.blacklist:
			self.func_blacklist = forward_thread.put_blacklist
		self.min_resolution = config.getint('forward', 'lowq_resolution', fallback=120)
		#self.min_resolution = eval(self.config['forward']['lowq_resolution']) if self.config.has_option('forward', 'lowq_resolution') else 120
		self.custom_switch = False
		#blacklist_keyword = eval(config['forward']['blacklist_keyword'])
		self.restart_require = False
		self.forward_thread = forward_thread(self.app)
		self.owner_group_id = config.getint('account', 'group_id', fallback=-1)

	def init_handle(self):
		self.app.add_handler(MessageHandler(self.get_msg_from_owner_group,			Filters.chat(self.owner_group_id) & Filters.reply))
		self.app.add_handler(MessageHandler(self.get_command_from_target,			Filters.chat(self.configure.predefined_group_list) & Filters.text & Filters.reply))
		#self.app.add_handler(MessageHandler(self.do_nothing, 						Filters.chat(self.configure.predefined_group_list)))
		self.app.add_handler(MessageHandler(self.pre_check, 						Filters.media & ~Filters.private & ~Filters.sticker & ~Filters.voice))
		self.app.add_handler(MessageHandler(self.handle_photo,						Filters.photo & ~Filters.private & ~Filters.chat([self.configure.photo, self.configure.lowq])))
		self.app.add_handler(MessageHandler(self.handle_video,						Filters.video & ~Filters.private & ~Filters.chat(self.configure.video)))
		self.app.add_handler(MessageHandler(self.handle_gif,						Filters.animation & ~Filters.private & ~Filters.chat(self.configure.gif)))
		self.app.add_handler(MessageHandler(self.handle_document,					Filters.document & ~Filters.private & ~Filters.chat(self.configure.doc)))
		self.app.add_handler(MessageHandler(self.handle_other,						Filters.media & ~Filters.private & ~Filters.sticker & ~Filters.voice))
		self.app.add_handler(MessageHandler(self.pre_private,						Filters.private))
		self.app.add_handler(MessageHandler(self.add_Except,						Filters.command('e') & Filters.private))
		self.app.add_handler(MessageHandler(self.process_query,						Filters.command('q') & Filters.private))
		self.app.add_handler(MessageHandler(self.add_BlackList,						Filters.command('b') & Filters.private))
		self.app.add_handler(MessageHandler(self.process_show_detail,				Filters.command('s') & Filters.private))
		self.app.add_handler(MessageHandler(self.set_forward_target_reply,			Filters.command('f') & Filters.reply & Filters.private))
		self.app.add_handler(MessageHandler(self.set_forward_target,				Filters.command('f') & Filters.private))
		self.app.add_handler(MessageHandler(self.add_user,							Filters.command('a') & Filters.private))
		self.app.add_handler(MessageHandler(self.change_code,						Filters.command('pw') & Filters.private))
		self.app.add_handler(MessageHandler(self.undo_blacklist_operation,			Filters.command('undo') & Filters.private))
		self.app.add_handler(MessageHandler(self.switch_detail2,					Filters.command('sd2') & Filters.private))
		self.app.add_handler(MessageHandler(self.switch_detail,						Filters.command('sd') & Filters.private))
		self.app.add_handler(MessageHandler(self.callstopfunc,						Filters.command('stop') & Filters.private))
		self.app.add_handler(MessageHandler(self.show_help_message,					Filters.command('help') & Filters.private))
		self.app.add_handler(MessageHandler(self.process_private,					Filters.private))

	def user_checker(self, msg: Message):
		self.redis.sismember('for_admin', msg.chat.id)

	def reply_checker_and_del_from_blacklist(self, client: Client, msg: Message):
		try:
			pending_del = None
			if msg.reply_to_message.text:
				r = re.match(r'^Add (-?\d+) to blacklist$', msg.reply_to_message.text)
				if r and msg.reply_to_message.from_user.id != msg.chat.id:
					pending_del = r.group(1)
			else:
				group_id = msg.forward_from.id if msg.forward_from else msg.forward_from_chat.id if msg.forward_from_chat else None
				if group_id and group_id in black_list:
					pending_del = group_id
			if pending_del is not None:
				if self.redis.srem('for_blacklist', pending_del):
					self.checker.remove_blacklist(pending_del)
				client.send_message(self.owner_group_id, 'Remove `{}` from blacklist'.format(group_id), parse_mode='markdown')
		except:
			if msg.reply_to_message.text: print(msg.reply_to_message.text)
			logger.exception('Catch!')

	def add_black_list(self, user_id: int, post_back_id=None):
		# Check is msg from authorized user
		if user_id is None or self.redis.sismember('for_admin', user_id):
			raise KeyError
		if self.redis.sadd('for_blacklist', user_id):
			self.checker.insert_blacklist(user_id)
		#if int(user_id) in black_list: return
		#if isinstance(user_id, bytes): user_id = user_id.decode()
		#black_list.append(int(user_id))
		#black_list = list(set(black_list))
		#config['forward']['black_list'] = repr(black_list)
		logger.info('Add %d to blacklist', user_id)
		#save_config_Thread(config)
		if post_back_id is not None:
			self.app.send_message(post_back_id, 'Add `{}` to blacklist'.format(user_id),
				parse_mode='markdown')

	def del_message_by_id(self, client: Client, msg: Message, send_message_to : int or str = None, forward_control: bool = True):
		if forward_control and self.configure.blacklist == '':
			logger.error('Request forward but blacklist channel not specified')
			return
		id_from_reply = get_forward_id(msg.reply_to_message)
		q = self.checker.query("SELECT * FROM `msg_detail` WHERE (`from_chat` = %s OR `from_user` = %s OR `from_forward` = %s) AND `to_chat` != %s",
			(id_from_reply, id_from_reply, id_from_reply, self.configure.blacklist))
		if send_message_to:
			_msg = client.send_message(send_message_to, 'Find {} message(s)'.format(len(q)))
		if forward_control:
			if send_message_to:
				typing = set_status_thread(client, send_message_to)
			for x in q:
				forward_thread.put_blacklist(x['to_chat'], x['to_msg'], msg_raw=build_log(
					x['from_chat'], x['from_id'], x['from_user'], x['from_forward']))
			while not forward_thread.queue.empty(): time.sleep(0.5)
			if send_message_to: typing.setOff()
		for x in q:
			try: client.delete_messages(x['to_chat'], x['to_msg'])
			except: pass
		self.checker.execute("DELETE FROM `msg_detail` WHERE (`from_chat` = %s OR `from_user` = %s OR `from_forward` = %s) AND `to_chat` != %s", (
			id_from_reply, id_from_reply, id_from_reply, self.configure.blacklist))
		if send_message_to:
			_msg.edit(f'Delete all message from `{id_from_reply}` completed.', 'markdown')

	def get_msg_from_owner_group(self, client: Client, msg: Message):
		try:
			if msg.text and msg.text == '/undo':
				self.reply_checker_and_del_from_blacklist(client, msg)
		except:
			# TODO: detail exception
			logger.exception('')

	def get_command_from_target(self, client: Client, msg: Message):
		if re.match(r'^\/(del(f)?|b|undo|print)$', msg.text):
			if msg.text == '/b':
				#client.delete_messages(msg.chat.id, msg.message_id)
				for_id = get_forward_id(msg.reply_to_message)
				#for_id = get_forward_id(msg['reply_to_message'])
				self.add_black_list(for_id, (client, self.owner_group_id))
				# To enable delete message, please add `delete other messages' privilege to bot
				call_delete_msg(30, client.delete_messages, msg.chat.id, (msg['message_id'], msg['reply_to_message']['message_id']))
			elif msg.text == '/undo':
				group_id = msg.reply_to_message.message_id if msg.reply_to_message else None
				if group_id:
					try:
						if self.redis.srem('for_admin', group_id):
							self.checker.remove_admin(group_id)
						#black_list.remove(group_id)
						#self.config['forward']['black_list'] = repr(black_list)
						client.send_message(self.owner_group_id, f'Remove `{group_id}` from blacklist', 'markdown')
					except ValueError:
						client.send_message(self.owner_group_id, f'`{group_id}` not in blacklist', 'markdown')
			elif msg.text == '/print' and msg.reply_to_message is not None:
				print(msg.reply_to_message)
			else:
				call_delete_msg(20, client.delete_messages, msg.chat.id, msg.message_id)
				if get_forward_id(msg.reply_to_message):
					self.del_message_by_id(client, msg, self.owner_group_id, msg.text[-1] == 'f')

	@staticmethod
	def get_file_id(msg: Message, _type: str) -> str:
		return getattr(msg, _type).file_id

	@staticmethod
	def get_file_type(msg: Message) -> str:
		return 'photo' if msg.photo else \
			'video' if msg.video else \
			'animation' if msg.animation else \
			'sticker' if msg.sticker else \
			'voice' if msg.voice else \
			'document' if msg.document else \
			'audio' if msg.audio else \
			'contact' if msg.contact else \
			'text' if msg.text else 'error'

	def pre_check(self, _client: Client, msg: Message):
		if self.redis.sismember('for_bypass', msg.chat.id) or not self.checker.checkFile(self.get_file_id(msg, self.get_file_type(msg))):
			return
		raise ContinuePropagation

	def blacklist_checker(self, msg: Message):
		return self.redis.sismember('for_blacklist', msg.chat.id) or \
				(msg.from_user and self.redis.sismember('for_blacklist', msg.from_user.id)) or \
				(msg.forward_from and self.redis.sismember('for_blacklist', msg.forward_from.id)) or \
				(msg.forward_from_chat and self.redis.sismember('for_blacklist', msg.forward_from_chat.id))

	@staticmethod
	def do_nothing(*args):
		pass

	def forward_msg(self, msg: Message, to: int, what: str = 'photo'):
		if self.blacklist_checker(msg):
			if msg.from_user and msg.from_user.id == 630175608: return
			self.func_blacklist(msg.chat.id, msg.message_id, log_struct(True, 'forward blacklist context %s from %s (id: %d)', what, msg.chat.title, msg.chat.id), msg)
			return
		forward_target = to
		spec_target = None if what == 'other' else self.redis.get(msg.chat.id)
		if spec_target is None:
			spec_target = self.redis.get(msg.forward_from_chat.id)
		if spec_target is not None:
			forward_target = getattr(self.configure, spec_target)
		elif is_bot(msg):
			forward_target = self.configure.bot
		self.forward_thread.put(forward_target,
			msg.chat.id, msg.message_id, log_struct(True, 'forward {} from {} (id: {})', what, msg.chat.title, msg['chat']['id']), msg)

	def handle_photo(self, _client: Client, msg: Message):
		self.forward_msg(msg, self.configure.photo if self.checker.check_photo(msg.photo.thumbs[-1]) else self.configure.lowq)
		#self.forward_msg(msg, self.config['forward']['to_photo'] if checker.check_photo(msg.photo) else self.config['forward']['to_lowq'])

	def handle_video(self, _client: Client, msg: Message):
		self.forward_msg(msg, self.configure.video, 'video')

	def handle_gif(self, _client: Client, msg: Message):
		self.forward_msg(msg, self.configure.gif, 'gif')

	def handle_document(self, _client: Client, msg: Message):
		if msg.document.file_name.split('.')[-1] in ('com', 'exe', 'bat', 'cmd'): return
		forward_target = self.configure.doc if '/' in msg.document.mime_type and msg.document.mime_type.split('/')[0] in ('image', 'video') else self.configure.other
		self.forward_msg(msg, forward_target, 'doc' if forward_target != self.configure.other else 'other')

	def handle_other(self, _client: Client, msg: Message):
		self.forward_msg(msg, self.configure.other, 'other')

	def pre_private(self, client: Client, msg: Message):
		if not self.user_checker(msg):
			client.send(api.functions.messages.ReportSpam(peer=client.resolve_peer(msg.chat.id)))
			return
		client.send(api.functions.messages.ReadHistory(peer=client.resolve_peer(msg.chat.id), max_id=msg.message_id))
		raise ContinuePropagation

	def add_Except(self, _client: Client, msg: Message):
		if len(msg.text) < 4:
			return
		#bypass_list.append(int(msg.text[3:]))
		if self.redis.sadd('for_bypass', msg.text[3:]):
			pass
		#bypass_list = list(set(bypass_list))
		#self.config['forward']['bypass_list'] = repr(bypass_list)
		msg.reply('Add `{}` to bypass list'.format(msg.text[3:]), parse_mode='markdown')
		logger.info('add except id: %s', msg.text[3:])

	def process_query(self, client: Client, msg: Message):
		r = re.match(r'^\/q (-?\d+)(d)?$', msg.text)
		if r is None:
			return
		get_history_process(client, msg.chat.id, r.group(1), dirty_run=r.group(2) is not None)

	def add_BlackList(self, client: Client, msg: Message):
		try: self.add_black_list(msg.text[3:])
		except:
			client.send_message(msg.chat.id, "Check your input")
			logger.exception('Catch!')

	def process_show_detail(self, _client: Client, msg: Message):
		self.echo_switch = not self.echo_switch
		msg.reply('Set echo to {}'.format(self.echo_switch))

	def set_forward_target_reply(self, _client: Client, msg: Message):
		if msg.reply_to_message.text is not None: return
		r = re.match(r'^forward_from = (-\d+)$', msg.reply_to_message.text)
		r1 = re.match(r'^\/f (other|photo|bot|video|anime|gif|doc|lowq)$', msg.text)
		if r is None or r1 is None: return
		self._set_forward_target(r.group(1), r1.group(1), msg)
		#do_spec_forward.update({int(r.group(1)): r1.group(1)})
		#self.config['forward']['special'] = repr(do_spec_forward)
		#self.checker.update_forward_target(r1.group(1), r.group(1))
		#self._set_forward_target(r1.group)
		#msg.reply('Set group `{}` forward to `{}`'.format(r.group(1), r1.group(1)), parse_mode='markdown')

	def set_forward_target(self, _client: Client, msg: Message):
		r = re.match(r'^\/f (-?\d+) (other|photo|bot|video|anime|gif|doc|lowq)$', msg.text)
		if r is None:
			return
		self._set_forward_target(r.group(1), r.group(2), msg)

	def _set_forward_target(self, chat_id: int, target: str, msg: Message):
		self.redis.set(chat_id, target)
		self.checker.update_forward_target(chat_id, target)
		msg.reply(f'Set group `{chat_id}` forward to `{target}`', parse_mode='markdown')

	def add_user(self, _client: Client, msg: Message):
		r = re.match(r'^/a (.+)$', msg.text)
		if r and r.group(1) == self.configure.authorized_code:
			if self.redis.sadd('for_admin', msg.chat.id):
				self.checker.insert_admin(msg.chat.id)
			msg.reply('Success add to authorized users.')

	def change_code(self, _client: Client, msg: Message):
		r = re.match(r'^/pw (.+)$', msg.text)
		if r:
			msg.reply('Success changed authorize code.')

	def undo_blacklist_operation(self, client: Client, msg: Message):
		self.reply_checker_and_del_from_blacklist(client, msg)

	def switch_detail2(self, _client: Client, msg: Message):
		self.custom_switch = not self.custom_switch
		msg.reply(f'Switch custom print to {self.custom_switch}')

	def switch_detail(self, _client: Client, msg: Message):
		self.detail_msg_switch = not self.detail_msg_switch
		msg.reply(f'Switch detail print to {self.detail_msg_switch}')

	def callstopfunc(self, _client: Client, msg: Message):
		#msg.reply('Exiting...')
		#Thread(target=process_exit.exit_process, args=(2,)).start()
		pass

	def show_help_message(self, _client: Client, msg: Message):
		msg.reply(""" Usage:
		/e <chat_id>            Add `chat_id' to bypass list
		/a <password>           Use the `password' to obtain authorization
		/q <chat_id>            Request to query one specific `chat_id'
		/b <chat_id>            Add `chat_id' to blacklist
		/s                      Toggle echo switch
		/f <chat_id> <target>   Add `chat_id' to specified forward rules
		/pw <new_password>      Change password to new password
		/stop                   Stop bot
		""", parse_mode='text')

	def process_private(self, _client: Client, msg: Message):
		if self.custom_switch:
			obj = getattr(msg, self.get_file_type(msg), None)
			if obj:
				msg.reply('```{}```\n{}'.format(str(obj), 'Resolution: `{}`'.format(msg.photo.file_size/(msg.photo.width * msg.photo.height)*1000) if msg.photo else ''), parse_mode='markdown')
		if self.echo_switch:
			msg.reply('forward_from = `{}`'.format(get_forward_id(msg, -1)), parse_mode='markdown')
			if self.detail_msg_switch: print(msg)
		if msg.text is None: return
		r = re.match(r'^Add (-?\d+) to blacklist$', msg.text)
		if r is None: return
		self.add_black_list(r.group(1), msg.chat.id)

	def start(self):
		self.app.start()

	def idle(self):
		self.app.idle()

	def stop(self):
		self.app.stop()
		forward_thread.switch = False
		checkfile.close_instance()


def call_delete_msg(interval: int, func, target_id: int, msg_: Message):
	_t = Timer(interval, func, (target_id, msg_))
	_t.daemon = True
	_t.start()

def main():
	bot = bot_controler()
	bot.start()
	try:
		bot.idle()
	except InterruptedError:
		pass
	bot.stop()


if __name__ == '__main__':
	main()