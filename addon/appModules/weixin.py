# -*- coding: utf-8 -*-
# PC WeChat add-on for NVDA
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.
# Copyright (C) 2025 Cary-rowen <manchen_0528@outlook.com>

from __future__ import annotations

from dataclasses import dataclass
from os.path import dirname, join
from typing import Any, NamedTuple

import addonHandler
import api
import appModuleHandler
import config
import controlTypes
import eventHandler
import mouseHandler
import speech
import ui
import UIAHandler
import winUser
import wx
from comInterfaces import UIAutomationClient as UIA

from logHandler import log
from nvwave import playWaveFile
from scriptHandler import script

addonHandler.initTranslation()


class MessageRecord(NamedTuple):
	"""Accessible text and identity for a visible WeChat message."""

	info: tuple[Any, ...]
	text: str


@dataclass
class ReviewState:
	"""Temporary message review queue state."""

	messages: list[str]
	currentIndex: int = -1


class AppModule(appModuleHandler.AppModule):
	"""App module for PC WeChat enhancements."""

	MESSAGE_LIST_UIA_ID = "chat_message_list"
	MESSAGE_INPUT_UIA_ID = "chat_input_field"
	MESSAGE_ITEM_UIA_ID = "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view"
	MESSAGE_TIME_ITEM_UIA_CLASS = "mmui::ChatItemView"
	MAIN_WINDOW_CLASS_NAME = "Qt51514QWindowIcon"
	CONFIG_SECTION = "weixin"
	CONFIG_KEY_NOTIFICATION_MODE = "notificationMode"
	MAX_MESSAGE_QUEUE_SIZE = 200
	MAX_SIMPLE_NEXT_TO_SEARCH = 80
	SCROLL_LOAD_DELAY = 25
	NOTIFICATION_SUPPRESSION_DELAY = 2000
	MAX_BOUNDARY_SCROLL_ATTEMPTS = 4
	KEYEVENTF_EXTENDEDKEY = 0x0001
	KEYEVENTF_KEYUP = 0x0002
	# Translators: The name of the category in NVDA's input gestures dialog.
	SCRIPT_CATEGORY = _("PC WeChat Enhancement")
	SOUND_NEW_MESSAGE = join(dirname(__file__), "popup.wav")

	# Translators: Reported when the current chat has no messages available for review.
	NO_MESSAGES_TEXT = _("No messages in the current chat.")

	MODE_OFF, MODE_SOUND_ONLY, MODE_SOUND_AND_SPEECH = range(3)
	QUEUE_UPDATE_UNCHANGED = "unchanged"
	QUEUE_UPDATE_INITIAL = "initial"
	QUEUE_UPDATE_APPEND = "append"
	QUEUE_UPDATE_PREPEND = "prepend"
	QUEUE_UPDATE_REPLACE = "replace"
	QUEUE_UPDATE_IGNORED = "ignored"

	confspec = {
		CONFIG_KEY_NOTIFICATION_MODE: f"integer(min=0, max=2, default={MODE_OFF})",
	}
	config.conf.spec[CONFIG_SECTION] = confspec

	def __init__(self, *args: Any, **kwargs: Any):
		super().__init__(*args, **kwargs)
		self.notificationMode = config.conf[self.CONFIG_SECTION][self.CONFIG_KEY_NOTIFICATION_MODE]
		self.isNotificationSuppressed = False
		self.notificationSuppressionTimer = None
		self.lastNotifiedMessageInfo = None
		self.reviewState = ReviewState(messages=[])
		self.activeMessageList = None
		self.reviewQueueUpdateOnLastRefresh = self.QUEUE_UPDATE_UNCHANGED
		self.scrollLoadTimer = None
		self.isBoundaryScrollPending = False

		eventHandler.requestEvents(
			"gainFocus",
			processId=self.processID,
			windowClassName=self.MAIN_WINDOW_CLASS_NAME,
		)

	def terminate(self):
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
		self.scrollLoadTimer = None
		if self.notificationSuppressionTimer and self.notificationSuppressionTimer.IsRunning():
			self.notificationSuppressionTimer.Stop()
		self.notificationSuppressionTimer = None
		super().terminate()

	def _findMessageListAfterInput(self, inputObj: Any) -> Any | None:
		obj = inputObj
		for _index in range(self.MAX_SIMPLE_NEXT_TO_SEARCH):
			try:
				obj = obj.simpleNext
			except Exception:
				return None
			if obj is None:
				return None
			if obj.UIAAutomationId == self.MESSAGE_LIST_UIA_ID:
				return obj
		return None

	def _findCurrentMessageList(self) -> Any | None:
		focus = api.getFocusObject()
		if focus is not None and focus.UIAAutomationId == self.MESSAGE_INPUT_UIA_ID:
			messageList = self._findMessageListAfterInput(focus)
			if messageList is not None:
				return messageList
		if (
			self.activeMessageList is not None
			and self.activeMessageList.UIAAutomationId == self.MESSAGE_LIST_UIA_ID
		):
			return self.activeMessageList
		return None

	def _getUIAMessageRecord(self, element: Any) -> MessageRecord | None:
		try:
			controlType = element.getCachedPropertyValue(UIA.UIA_ControlTypePropertyId)
			automationID = element.getCachedPropertyValue(UIA.UIA_AutomationIdPropertyId)
			className = element.getCachedPropertyValue(UIA.UIA_ClassNamePropertyId)
			text = element.getCachedPropertyValue(UIA.UIA_NamePropertyId)
			boundingRectangle = element.getCachedPropertyValue(UIA.UIA_BoundingRectanglePropertyId)
		except Exception:
			return None
		notSupportedValue = UIAHandler.handler.reservedNotSupportedValue
		if controlType == notSupportedValue:
			return None
		if controlType != UIA.UIA_ListItemControlTypeId:
			return None
		if automationID == notSupportedValue:
			automationID = None
		if className == notSupportedValue:
			className = None
		if automationID != self.MESSAGE_ITEM_UIA_ID and className != self.MESSAGE_TIME_ITEM_UIA_CLASS:
			return None
		if text == notSupportedValue:
			text = None
		if not text or speech.isBlank(text):
			return None
		if boundingRectangle == notSupportedValue or not boundingRectangle:
			location = None
		else:
			location = tuple(int(value) for value in boundingRectangle)
		itemID = automationID or className
		return MessageRecord(
			info=("uia", itemID, location, text),
			text=text,
		)

	def _collectVisibleMessageRecords(
		self,
		messageList: Any,
	) -> list[MessageRecord]:
		messageListElement = messageList.UIAElement
		try:
			childrenCacheRequest = UIAHandler.handler.baseCacheRequest.clone()
			childrenCacheRequest.addProperty(UIA.UIA_ControlTypePropertyId)
			childrenCacheRequest.addProperty(UIA.UIA_NamePropertyId)
			childrenCacheRequest.addProperty(UIA.UIA_AutomationIdPropertyId)
			childrenCacheRequest.addProperty(UIA.UIA_BoundingRectanglePropertyId)
			childrenCacheRequest.addProperty(UIA.UIA_ClassNamePropertyId)
			childrenCacheRequest.TreeScope = UIAHandler.TreeScope_Children
			cachedChildren = messageListElement.buildUpdatedCache(
				childrenCacheRequest,
			).getCachedChildren()
		except Exception:
			return []
		if not cachedChildren:
			return []
		records = []
		for index in range(cachedChildren.length):
			record = self._getUIAMessageRecord(cachedChildren.getElement(index))
			if record is not None:
				records.append(record)
		return records

	def _findSubList(self, source: list[str], target: list[str]) -> int | None:
		if not target or len(target) > len(source):
			return None
		for index in range(len(source) - len(target) + 1):
			if source[index:index + len(target)] == target:
				return index
		return None

	def _getSuffixPrefixOverlap(self, left: list[str], right: list[str]) -> int:
		maxOverlap = min(len(left), len(right))
		for count in range(maxOverlap, 0, -1):
			if left[-count:] == right[:count]:
				return count
		return 0

	def _mergeVisibleMessages(
		self,
		state: ReviewState,
		visibleMessages: list[str],
	) -> str:
		"""Merge visible messages into the temporary queue.

		@return: A queue update kind describing how the queue changed.
		"""
		oldMessages = state.messages
		if not oldMessages:
			state.messages = visibleMessages
			state.currentIndex = len(visibleMessages) - 1
			self._trimReviewState(state)
			return self.QUEUE_UPDATE_INITIAL

		oldIndex = state.currentIndex
		wasAtLast = oldIndex >= len(oldMessages) - 1
		newMessages = None
		indexOffset = 0
		updateKind = self.QUEUE_UPDATE_UNCHANGED

		visibleStart = self._findSubList(oldMessages, visibleMessages)
		if visibleStart is not None:
			return self.QUEUE_UPDATE_UNCHANGED

		oldStart = self._findSubList(visibleMessages, oldMessages)
		if oldStart is not None:
			newMessages = visibleMessages
			indexOffset = oldStart
			if len(visibleMessages) > len(oldMessages):
				if oldStart == 0:
					updateKind = self.QUEUE_UPDATE_APPEND
				else:
					updateKind = self.QUEUE_UPDATE_PREPEND
		else:
			appendOverlap = self._getSuffixPrefixOverlap(oldMessages, visibleMessages)
			prependOverlap = self._getSuffixPrefixOverlap(visibleMessages, oldMessages)
			if appendOverlap >= prependOverlap and appendOverlap > 0:
				newMessages = oldMessages + visibleMessages[appendOverlap:]
				updateKind = self.QUEUE_UPDATE_APPEND
			elif prependOverlap > 0:
				prependCount = len(visibleMessages) - prependOverlap
				newMessages = visibleMessages[:prependCount] + oldMessages
				indexOffset = prependCount
				updateKind = self.QUEUE_UPDATE_PREPEND
			else:
				if self.isBoundaryScrollPending:
					return self.QUEUE_UPDATE_IGNORED
				newMessages = visibleMessages
				updateKind = self.QUEUE_UPDATE_REPLACE

		state.messages = newMessages
		if wasAtLast or updateKind == self.QUEUE_UPDATE_REPLACE:
			state.currentIndex = len(newMessages) - 1
		else:
			state.currentIndex = min(oldIndex + indexOffset, len(newMessages) - 1)
		self._trimReviewState(state)
		return updateKind

	def _trimReviewState(self, state: ReviewState):
		overflow = len(state.messages) - self.MAX_MESSAGE_QUEUE_SIZE
		if overflow <= 0:
			return
		del state.messages[:overflow]
		state.currentIndex = max(0, state.currentIndex - overflow)

	def refreshMessageQueue(
		self,
		messageList: Any | None = None,
		setNotificationBaseline: bool = False,
	) -> ReviewState:
		self.reviewQueueUpdateOnLastRefresh = self.QUEUE_UPDATE_UNCHANGED
		if messageList is None:
			messageList = self._findCurrentMessageList()
		if messageList is None:
			return self.reviewState
		records = self._collectVisibleMessageRecords(messageList)
		if not records:
			return self.reviewState
		state = self.reviewState
		visibleMessages = [record.text for record in records]
		self.reviewQueueUpdateOnLastRefresh = self._mergeVisibleMessages(state, visibleMessages)
		self.activeMessageList = messageList
		if setNotificationBaseline:
			lastRecord = records[-1]
			self.lastNotifiedMessageInfo = (lastRecord.info, lastRecord.text)
		return state

	def _getMouseScrollPoint(self, messageList: Any) -> tuple[int, int] | None:
		left, top, width, height = messageList.location
		if width <= 0 or height <= 0:
			return None
		return int(left + width / 2), int(top + height / 2)

	def _isKeyDown(self, vkCode: int) -> bool:
		return bool(winUser.getAsyncKeyState(vkCode) & 32768)

	def _getPressedAltKeys(self) -> list[tuple[int, int]]:
		pressedKeys = []
		altKeyFlags = (
			(winUser.VK_LMENU, 0),
			(winUser.VK_RMENU, self.KEYEVENTF_EXTENDEDKEY),
		)
		for vkCode, flags in altKeyFlags:
			if self._isKeyDown(vkCode):
				pressedKeys.append((vkCode, flags))
		if not pressedKeys and self._isKeyDown(winUser.VK_MENU):
			pressedKeys.append((winUser.VK_MENU, 0))
		return pressedKeys

	def _setSyntheticAltKeysUp(self, pressedKeys: list[tuple[int, int]], isKeyUp: bool):
		keyUpFlag = self.KEYEVENTF_KEYUP if isKeyUp else 0
		for vkCode, flags in pressedKeys:
			winUser.keybd_event(vkCode, 0, flags | keyUpFlag, 0)

	def _scrollMessageList(self, messageList: Any, scrollSteps: int) -> bool:
		"""Scroll the message list by moving the mouse to the list temporarily."""
		point = self._getMouseScrollPoint(messageList)
		if point is None:
			return False
		oldX, oldY = winUser.getCursorPos()
		pressedAltKeys = self._getPressedAltKeys()
		try:
			if pressedAltKeys:
				self._setSyntheticAltKeysUp(pressedAltKeys, True)
			winUser.setCursorPos(*point)
			mouseHandler.scrollMouseWheel(scrollSteps, isVertical=True)
		except Exception:
			log.debugWarning("Unable to scroll the WeChat message list.", exc_info=True)
			return False
		finally:
			if pressedAltKeys:
				try:
					self._setSyntheticAltKeysUp(list(reversed(pressedAltKeys)), False)
				except Exception:
					log.debugWarning("Unable to restore Alt after WeChat message list scroll.", exc_info=True)
			try:
				winUser.setCursorPos(oldX, oldY)
			except Exception:
				pass
		return True

	def _speakMessageAtIndex(self, state: ReviewState, index: int) -> bool:
		if index < 0 or index >= len(state.messages):
			return False
		state.currentIndex = index
		ui.message(state.messages[index])
		return True

	def _getBoundaryTargetIndex(
		self,
		state: ReviewState,
		oldMessages: list[str],
		direction: int,
	) -> int | None:
		oldMessagesStart = self._findSubList(state.messages, oldMessages)
		if oldMessagesStart is None:
			return None
		if direction < 0 and oldMessagesStart > 0:
			return oldMessagesStart - 1
		if direction > 0 and oldMessagesStart + len(oldMessages) < len(state.messages):
			return oldMessagesStart + len(oldMessages)
		return None

	def _readAfterBoundaryScroll(
		self,
		direction: int,
		oldMessages: list[str],
		oldIndex: int,
		nextAttempt: int,
	):
		"""Refresh after a boundary scroll and read the requested relative message."""
		scheduledRetry = False
		try:
			if not self._isMessageInputFocus():
				return
			state = self.refreshMessageQueue(setNotificationBaseline=True)
			if not state.messages:
				return

			targetIndex = self._getBoundaryTargetIndex(state, oldMessages, direction)
			if targetIndex is not None:
				if self._speakMessageAtIndex(state, targetIndex):
					return
			if nextAttempt <= self.MAX_BOUNDARY_SCROLL_ATTEMPTS:
				scheduledRetry = True
				self._scrollBoundaryAndRead(state, direction, attempt=nextAttempt)
				return

			oldMessagesStart = self._findSubList(state.messages, oldMessages)
			if oldMessagesStart is not None:
				state.currentIndex = oldMessagesStart + oldIndex
			return

		finally:
			if not scheduledRetry:
				self.isBoundaryScrollPending = False

	def _performScrollAttempt(self, messageList: Any, direction: int, attempt: int) -> bool:
		if attempt >= 2:
			wheelUnits = 8
		elif attempt >= 1:
			wheelUnits = 6
		else:
			wheelUnits = 4
		scrollSteps = winUser.WHEEL_DELTA * wheelUnits
		if direction > 0:
			scrollSteps = -scrollSteps
		if 0 <= attempt <= self.MAX_BOUNDARY_SCROLL_ATTEMPTS:
			return self._scrollMessageList(messageList, scrollSteps)
		return False

	def _scrollBoundaryAndRead(self, state: ReviewState, direction: int, attempt: int = 0):
		"""Scroll beyond the visible boundary and read after WeChat updates the list."""
		self._suppressNotificationsForUserAction()
		messageList = self._findCurrentMessageList()
		if messageList is None:
			self.isBoundaryScrollPending = False
			return

		oldMessages = list(state.messages)
		oldIndex = state.currentIndex
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
		self.isBoundaryScrollPending = True

		if not self._performScrollAttempt(messageList, direction, attempt):
			if attempt < self.MAX_BOUNDARY_SCROLL_ATTEMPTS:
				self._scrollBoundaryAndRead(state, direction, attempt=attempt + 1)
				return
			self.isBoundaryScrollPending = False
			return

		self.scrollLoadTimer = wx.CallLater(
			self.SCROLL_LOAD_DELAY,
			self._readAfterBoundaryScroll,
			direction,
			oldMessages,
			oldIndex,
			attempt + 1,
		)

	def event_valueChange(self, obj: Any, nextHandler: Any):
		"""
		Handle chat message list scrollbar changes.

		The event updates the review queue and reports only tail-appended
		messages as automatic new-message notifications.
		"""
		if (
			obj.role != controlTypes.Role.SCROLLBAR
			or not obj.parent
			or obj.parent.UIAAutomationId != self.MESSAGE_LIST_UIA_ID
		):
			return nextHandler()

		messageList = obj.parent
		self.refreshMessageQueue(messageList)
		queueUpdateKind = self.reviewQueueUpdateOnLastRefresh

		latestMessage = messageList.lastChild
		automationID = latestMessage.UIAAutomationId
		className = latestMessage.UIAElement.CachedClassName
		if (
			latestMessage.role != controlTypes.Role.LISTITEM
			or (
				automationID != self.MESSAGE_ITEM_UIA_ID
				and className != self.MESSAGE_TIME_ITEM_UIA_CLASS
			)
		):
			return nextHandler()
		text = latestMessage.name
		if not text or speech.isBlank(text):
			return nextHandler()
		messageInfo = (
			("uia", automationID or className, tuple(latestMessage.location), text),
			text,
		)
		if messageInfo == self.lastNotifiedMessageInfo:
			return nextHandler()

		if (
			not self._shouldSuppressNewMessageNotification()
			and queueUpdateKind == self.QUEUE_UPDATE_APPEND
			and self.notificationMode != self.MODE_OFF
		):
			self.performNotification(latestMessage)
		self.lastNotifiedMessageInfo = messageInfo
		nextHandler()

	def event_gainFocus(self, obj: Any, nextHandler: Any):
		"""
		Handle focus changes in WeChat.

		Entering the message list activates the temporary review queue and marks
		the current newest message as the notification baseline.
		"""
		parent = obj.parent
		if (
			obj.role == controlTypes.Role.LISTITEM
			and parent
			and parent.UIAAutomationId == self.MESSAGE_LIST_UIA_ID
			and (
				obj.UIAAutomationId == self.MESSAGE_ITEM_UIA_ID
				or obj.UIAElement.CachedClassName == self.MESSAGE_TIME_ITEM_UIA_CLASS
			)
		):
			self._suppressNotificationsForUserAction()
			self.refreshMessageQueue(parent, setNotificationBaseline=True)
		elif obj.UIAAutomationId == self.MESSAGE_LIST_UIA_ID:
			self.refreshMessageQueue(obj, setNotificationBaseline=True)
		elif obj.UIAAutomationId == self.MESSAGE_INPUT_UIA_ID:
			self.refreshMessageQueue(setNotificationBaseline=True)
		nextHandler()

	def _suppressNotificationsForUserAction(self):
		self.isNotificationSuppressed = True
		if self.notificationSuppressionTimer and self.notificationSuppressionTimer.IsRunning():
			self.notificationSuppressionTimer.Restart(self.NOTIFICATION_SUPPRESSION_DELAY)
		else:
			self.notificationSuppressionTimer = wx.CallLater(
				self.NOTIFICATION_SUPPRESSION_DELAY,
				self._clearNotificationSuppression,
			)

	def _clearNotificationSuppression(self):
		self.isNotificationSuppressed = False

	def _shouldSuppressNewMessageNotification(self) -> bool:
		return self.isNotificationSuppressed or self.isBoundaryScrollPending

	def performNotification(self, messageObj: Any):
		playWaveFile(self.SOUND_NEW_MESSAGE)
		if self.notificationMode == self.MODE_SOUND_AND_SPEECH:
			ui.message(messageObj.name)

	def _isMessageInputFocus(self) -> bool:
		focus = api.getFocusObject()
		return focus is not None and focus.UIAAutomationId == self.MESSAGE_INPUT_UIA_ID

	def _cancelPendingBoundaryReview(self):
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
		self.scrollLoadTimer = None
		self.isBoundaryScrollPending = False

	def _sendReviewGestureThrough(self, gesture: Any):
		self._cancelPendingBoundaryReview()
		gesture.send()

	def _readReviewDirection(self, state: ReviewState, direction: int) -> bool:
		if direction > 0 and state.currentIndex >= len(state.messages) - 1:
			return self._speakMessageAtIndex(state, state.currentIndex)
		nextIndex = state.currentIndex + direction
		if nextIndex < 0 or nextIndex >= len(state.messages):
			self._scrollBoundaryAndRead(state, direction)
			return False
		return self._speakMessageAtIndex(state, nextIndex)

	def _getActiveReviewState(self) -> ReviewState | None:
		state = self.reviewState
		if not state.messages:
			state = self.refreshMessageQueue(setNotificationBaseline=True)
		if state.messages:
			return state
		ui.message(self.NO_MESSAGES_TEXT)
		return None

	def _handleRelativeReviewGesture(
		self,
		gesture: Any,
		direction: int,
	):
		if not self._isMessageInputFocus():
			self._sendReviewGestureThrough(gesture)
			return
		if self.isBoundaryScrollPending:
			return
		self._suppressNotificationsForUserAction()
		state = self._getActiveReviewState()
		if state is None:
			return
		self._readReviewDirection(state, direction)

	def _readIndexedReviewMessage(self, gesture: Any, index: int):
		if not self._isMessageInputFocus():
			self._sendReviewGestureThrough(gesture)
			return
		self._suppressNotificationsForUserAction()
		self._cancelPendingBoundaryReview()
		state = self._getActiveReviewState()
		if state is None:
			return
		if index < 0:
			index = len(state.messages) + index
		self._speakMessageAtIndex(state, index)

	@script(
		# Translators: Description for the command that reads the previous WeChat message.
		description=_("Reads the previous message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+upArrow",
	)
	def script_readPreviousMessage(self, gesture: Any):
		self._handleRelativeReviewGesture(gesture, -1)

	@script(
		# Translators: Description for the command that reads the next WeChat message.
		description=_("Reads the next message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+downArrow",
	)
	def script_readNextMessage(self, gesture: Any):
		self._handleRelativeReviewGesture(gesture, 1)

	@script(
		# Translators: Description for the command that reads the first WeChat message.
		description=_("Reads the first message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+home",
	)
	def script_readFirstMessage(self, gesture: Any):
		self._readIndexedReviewMessage(gesture, 0)

	@script(
		# Translators: Description for the command that reads the last WeChat message.
		description=_("Reads the last message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+end",
	)
	def script_readLastMessage(self, gesture: Any):
		self._readIndexedReviewMessage(gesture, -1)

	@script(
		# Translators: Description for the command that changes new message notification mode.
		description=_("Cycles through new message notification modes (Off -> Sound Only -> Sound and Speech)"),
		category=SCRIPT_CATEGORY,
		gesture="kb:f3",
	)
	def script_toggleNotificationMode(self, gesture: Any):
		self.notificationMode = (self.notificationMode + 1) % 3
		config.conf[self.CONFIG_SECTION][self.CONFIG_KEY_NOTIFICATION_MODE] = self.notificationMode
		modeMessages = (
			_("Off"),
			_("Sound Only"),
			_("Sound and speech"),
		)
		ui.message(modeMessages[self.notificationMode])
		self.refreshMessageQueue(setNotificationBaseline=True)
