# -*- coding: utf-8 -*-
# PC WeChat add-on for NVDA
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.
# Copyright (C) 2025 Cary-rowen <manchen_0528@outlook.com>

from os.path import dirname, join

import addonHandler
import appModuleHandler
import config
import controlTypes
import eventHandler
import speech
import ui  # Import ui for messaging capabilities
import wx

from logHandler import log
from nvwave import playWaveFile
from scriptHandler import script

addonHandler.initTranslation()

class AppModule(appModuleHandler.AppModule):

	# --- Constants ---
	MESSAGE_LIST_UIA_ID = 'chat_message_list'
	MAIN_TABBAR_UIA_ID = "main_tabbar"
	MAIN_WINDOW_CLASS_NAME = "Qt51514QWindowToolSaveBits"
	CONFIG_SECTION = "weixin"
	CONFIG_KEY_NOTIFICATION_MODE = "notificationMode"
	SCRIPT_CATEGORY = _("PC WeChat Enhancement")
	SOUND_NEW_MESSAGE = join(dirname(__file__), "popup.wav")

	# Notification mode
	MODE_OFF, MODE_SOUND_ONLY, MODE_SOUND_AND_SPEECH = range(3)

	confspec = {
		CONFIG_KEY_NOTIFICATION_MODE: f"integer(min=0, max=2, default={MODE_OFF})"
	}
	config.conf.spec[CONFIG_SECTION] = confspec

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.notification_mode = config.conf[self.CONFIG_SECTION][self.CONFIG_KEY_NOTIFICATION_MODE]
		self.last_read_message_info = None
		self.is_user_active = False
		self._activity_timer = None
		# Report global search results
		eventHandler.requestEvents("gainFocus", self.processID, self.MAIN_WINDOW_CLASS_NAME)

	def event_valueChange(self, obj, nextHandler):
		"""
		Handles the valueChange event from the scrollbar, triggering new message
		notifications only when the user is not considered active.
		"""
		#Is the feature enabled and is the user idle?
		if self.notification_mode == self.MODE_OFF or self.is_user_active:
			return nextHandler()

		# Is the object a scrollbar?
		if obj.role != controlTypes.Role.SCROLLBAR:
			return nextHandler()

		# this scrollbar belong to the chat message list?
		try:
			if not obj.parent or obj.parent.UIAAutomationId != self.MESSAGE_LIST_UIA_ID:
				log.debug("Ignoring valueChange: Event did not originate from the target message list scrollbar.")
				return nextHandler()
		except Exception:
			return nextHandler()

		# Is the message content new?
		try:
			latest_message = obj.parent.lastChild
			if not latest_message:
				return nextHandler()

			current_message_info = (latest_message.location, latest_message.name)
			if current_message_info == self.last_read_message_info:
				log.debug("Ignoring valueChange: Message content is a duplicate of the last one read.")
				return nextHandler()
		except Exception as e:
			log.error(f"Error while validating latest message: {e}", exc_info=True)
			return nextHandler()

		log.debug(f"New message validated. Name: '{latest_message.name}'")
		self.perform_notification(latest_message)
		self.last_read_message_info = current_message_info
		nextHandler()


	def event_gainFocus(self, obj, nextHandler):
		"""
		Handles focus events to:
		1. Mark the user as "active" when a message item is focused.
		2. Announce the first item when the main nav bar is focused.
		TODO: The curren item  that are actually focused on should be announced
		"""
		if obj.role == controlTypes.Role.TOOLBAR and obj.UIAAutomationId == self.MAIN_TABBAR_UIA_ID:
			wx.CallLater(0, lambda: speech.speakObject(obj.simpleFirstChild))

		try:
			is_message_item = (obj.role == controlTypes.Role.LISTITEM and
							   obj.parent and
							   obj.parent.UIAAutomationId == self.MESSAGE_LIST_UIA_ID)
		except Exception:
			is_message_item = False

		if is_message_item:
			self.notifyUserActivity()
		nextHandler()

	def notifyUserActivity(self):
		"""Sets the user as active and manages a 2-second cooldown timer."""
		self.is_user_active = True
		if self._activity_timer and self._activity_timer.IsRunning():
			self._activity_timer.Restart(2000)
		else:
			self._activity_timer = wx.CallLater(2000, self._clearUserActivityFlag)
		log.debug("User activity detected (message item focused), auto-reading paused for 2 seconds.")

	def _clearUserActivityFlag(self):
		self.is_user_active = False
		log.debug("User activity timeout. Resuming auto-reading.")

	def perform_notification(self, message_obj):
		playWaveFile(self.SOUND_NEW_MESSAGE)
		if self.notification_mode == self.MODE_SOUND_AND_SPEECH:
			ui.message(message_obj.name)


	@script(
		description=_("Cycles through new message notification modes (Off -> Sound Only -> Sound and Speech)"),
		category=SCRIPT_CATEGORY,
		gesture="kb:f3"
	)
	def script_toggleNotificationMode(self, gesture):
		self.notification_mode = (self.notification_mode + 1) % 3
		config.conf[self.CONFIG_SECTION][self.CONFIG_KEY_NOTIFICATION_MODE] = self.notification_mode
		mode_messages = {
			self.MODE_OFF: _("Off"),
			self.MODE_SOUND_ONLY: _("Sound Only"),
			self.MODE_SOUND_AND_SPEECH: _("Sound and speech"),
		}
		ui.message(mode_messages.get(self.notification_mode))
		self.last_read_message_info = None
