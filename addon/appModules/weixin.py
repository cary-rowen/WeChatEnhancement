# -*- coding: utf-8 -*-
# PC WeChat add-on for NVDA
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.
# Copyright (C) 2025 Cary-rowen <manchen_0528@outlook.com>

from __future__ import annotations

from os.path import dirname, join
from typing import Any

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


class AppModule(appModuleHandler.AppModule):
	"""App module for PC WeChat enhancements."""

	MESSAGE_LIST_UIA_ID = "chat_message_list"
	MESSAGE_INPUT_UIA_ID = "chat_input_field"
	MESSAGE_ITEM_UIA_ID = "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view"
	MESSAGE_TIME_ITEM_UIA_CLASS = "mmui::ChatItemView"
	MAIN_TABBAR_UIA_ID = "main_tabbar"
	MAIN_WINDOW_CLASS_NAME = "Qt51514QWindowIcon"
	CONFIG_SECTION = "weixin"
	CONFIG_KEY_NOTIFICATION_MODE = "notificationMode"
	MAX_MESSAGE_QUEUE_SIZE = 200
	MAX_SIMPLE_NEXT_TO_SEARCH = 80
	SCROLL_LOAD_DELAY = 25
	MAX_BOUNDARY_SCROLL_ATTEMPTS = 4
	KEYEVENTF_EXTENDEDKEY = 0x0001
	KEYEVENTF_KEYUP = 0x0002
	# Translators: The name of the category in NVDA's input gestures dialog.
	SCRIPT_CATEGORY = _("PC WeChat Enhancement")
	SOUND_NEW_MESSAGE = join(dirname(__file__), "popup.wav")

	# Translators: Reported when the current chat has no messages available for review.
	NO_MESSAGES_TEXT = _("No messages in the current chat.")

	MODE_OFF, MODE_SOUND_ONLY, MODE_SOUND_AND_SPEECH = range(3)

	confspec = {
		CONFIG_KEY_NOTIFICATION_MODE: f"integer(min=0, max=2, default={MODE_OFF})",
	}
	config.conf.spec[CONFIG_SECTION] = confspec

	def __init__(self, *args: Any, **kwargs: Any):
		"""Initialize notification and message review state."""
		super().__init__(*args, **kwargs)
		self.notificationMode = config.conf[self.CONFIG_SECTION][self.CONFIG_KEY_NOTIFICATION_MODE]
		self.isUserActive = False
		self.activityTimer = None
		self.reviewState = self._createReviewState()
		self.activeMessageList = None
		self.reviewQueueResetOnLastRefresh = False
		self.scrollLoadTimer = None
		self.isBoundaryScrollPending = False

		eventHandler.requestEvents(
			"gainFocus",
			processId=self.processID,
			windowClassName=self.MAIN_WINDOW_CLASS_NAME,
		)

	def terminate(self):
		"""Stop pending timers before unloading the app module."""
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
		self.scrollLoadTimer = None
		if self.activityTimer and self.activityTimer.IsRunning():
			self.activityTimer.Stop()
		self.activityTimer = None
		super().terminate()

	def _getObjectText(self, obj: Any) -> str | None:
		"""Return useful accessible text for an object."""
		try:
			text = obj.name
		except Exception:
			return None
		if not text or speech.isBlank(text):
			return None
		return text

	def _getObjectAutomationID(self, obj: Any) -> str:
		"""Return an object's AutomationId when it is available."""
		try:
			automationID = obj.UIAAutomationId
		except Exception:
			return ""
		return automationID or ""

	def _getObjectUIAClassName(self, obj: Any) -> str:
		"""Return an object's UIA class name when it is available."""
		try:
			className = obj.UIAElement.CachedClassName
		except Exception:
			return ""
		return className or ""

	def _getObjectLocation(self, obj: Any) -> tuple[int, int, int, int] | None:
		"""Return an object's screen location."""
		try:
			location = obj.location
		except Exception:
			return None

		try:
			left, top, width, height = location
		except Exception:
			try:
				left = location.left
				top = location.top
				width = location.width
				height = location.height
			except Exception:
				return None
		return (int(left), int(top), int(width), int(height))

	def _getUIARectLocation(self, rect: Any) -> tuple[int, int, int, int] | None:
		"""Return a UIA bounding rectangle as an integer location tuple."""
		if not rect:
			return None
		try:
			left, top, width, height = rect
		except Exception:
			return None
		return (int(left), int(top), int(width), int(height))

	def _isMessageItem(self, obj: Any) -> bool:
		"""Return whether an object should be included in the message queue."""
		try:
			if obj.role != controlTypes.Role.LISTITEM:
				return False
		except Exception:
			return False
		return (
			self._getObjectAutomationID(obj) == self.MESSAGE_ITEM_UIA_ID
			or self._getObjectUIAClassName(obj) == self.MESSAGE_TIME_ITEM_UIA_CLASS
		)

	def _getMessageRecord(self, obj: Any) -> dict[str, Any] | None:
		"""Return a stable record for a message object."""
		if not self._isMessageItem(obj):
			return None

		text = self._getObjectText(obj)
		if text is None:
			return None

		automationID = self._getObjectAutomationID(obj)
		className = self._getObjectUIAClassName(obj)
		itemID = automationID or className

		return {
			"info": ("uia", itemID, self._getObjectLocation(obj), text),
			"text": text,
		}

	def _isMessageList(self, obj: Any) -> bool:
		"""Return whether an object is the WeChat chat message list."""
		return self._getObjectAutomationID(obj) == self.MESSAGE_LIST_UIA_ID

	def _findMessageListAfterInput(self, inputObj: Any) -> Any | None:
		"""Find the chat message list by walking forward from the input field."""
		obj = inputObj
		for _index in range(self.MAX_SIMPLE_NEXT_TO_SEARCH):
			try:
				obj = obj.simpleNext
			except Exception:
				return None
			if obj is None:
				return None
			if self._isMessageList(obj):
				return obj
		return None

	def _findCurrentMessageList(self) -> Any | None:
		"""Return the current message list for input-field review."""
		try:
			focus = api.getFocusObject()
		except Exception:
			focus = None

		if focus is not None and self._isMessageInputObject(focus):
			messageList = self._findMessageListAfterInput(focus)
			if messageList is not None:
				return messageList

		if self.activeMessageList is not None and self._isMessageList(self.activeMessageList):
			return self.activeMessageList

		return None

	def _getCachedUIAProperty(self, element: Any, propertyID: int) -> Any:
		"""Return a cached UIA property value, ignoring unsupported values."""
		try:
			value = element.getCachedPropertyValue(propertyID)
		except Exception:
			return None
		try:
			if value == UIAHandler.handler.reservedNotSupportedValue:
				return None
		except Exception:
			pass
		return value

	def _getUIAMessageRecord(self, element: Any) -> dict[str, Any] | None:
		"""Return a message record from a cached UIA element."""
		controlType = self._getCachedUIAProperty(element, UIA.UIA_ControlTypePropertyId)
		if controlType != UIA.UIA_ListItemControlTypeId:
			return None

		automationID = self._getCachedUIAProperty(element, UIA.UIA_AutomationIdPropertyId)
		className = self._getCachedUIAProperty(element, UIA.UIA_ClassNamePropertyId)
		if automationID != self.MESSAGE_ITEM_UIA_ID and className != self.MESSAGE_TIME_ITEM_UIA_CLASS:
			return None

		text = self._getCachedUIAProperty(element, UIA.UIA_NamePropertyId)
		if not text or speech.isBlank(text):
			return None

		location = self._getUIARectLocation(
			self._getCachedUIAProperty(element, UIA.UIA_BoundingRectanglePropertyId),
		)
		itemID = automationID or className
		return {
			"info": ("uia", itemID, location, text),
			"text": text,
		}

	def _collectVisibleMessageRecords(
		self,
		messageList: Any,
	) -> list[dict[str, Any]]:
		"""Collect visible message records directly from cached UIA children."""
		try:
			messageListElement = messageList.UIAElement
		except Exception:
			return []

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
			try:
				record = self._getUIAMessageRecord(cachedChildren.getElement(index))
			except Exception:
				record = None
			if record is not None:
				records.append(record)
		return records

	def _createReviewState(self) -> dict[str, Any]:
		"""Create temporary message review state."""
		return {
			"messages": [],
			"currentIndex": -1,
			"lastReadMessageInfo": None,
			"visibleStart": None,
			"visibleEnd": None,
		}

	def _findSubList(self, source: list[str], target: list[str]) -> int | None:
		"""Return the start index of a contiguous target list inside source."""
		if not target or len(target) > len(source):
			return None
		for index in range(len(source) - len(target) + 1):
			if source[index:index + len(target)] == target:
				return index
		return None

	def _getSuffixPrefixOverlap(self, left: list[str], right: list[str]) -> int:
		"""Return the largest overlap between left's suffix and right's prefix."""
		maxOverlap = min(len(left), len(right))
		for count in range(maxOverlap, 0, -1):
			if left[-count:] == right[:count]:
				return count
		return 0

	def _mergeVisibleMessages(
		self,
		state: dict[str, Any],
		visibleMessages: list[str],
	) -> bool:
		"""Merge visible messages into the temporary queue.

		@return: True when the queue was reset to an unrelated visible window.
		"""
		oldMessages = state["messages"]
		if not oldMessages:
			state["messages"] = visibleMessages
			state["currentIndex"] = len(visibleMessages) - 1
			self._trimReviewState(state)
			return True

		oldIndex = state["currentIndex"]
		wasAtLast = oldIndex >= len(oldMessages) - 1
		newMessages = None
		indexOffset = 0
		didReplace = False

		visibleStart = self._findSubList(oldMessages, visibleMessages)
		if visibleStart is not None:
			return False

		oldStart = self._findSubList(visibleMessages, oldMessages)
		if oldStart is not None:
			newMessages = visibleMessages
			indexOffset = oldStart
		else:
			appendOverlap = self._getSuffixPrefixOverlap(oldMessages, visibleMessages)
			prependOverlap = self._getSuffixPrefixOverlap(visibleMessages, oldMessages)
			if appendOverlap >= prependOverlap and appendOverlap > 0:
				newMessages = oldMessages + visibleMessages[appendOverlap:]
			elif prependOverlap > 0:
				prependCount = len(visibleMessages) - prependOverlap
				newMessages = visibleMessages[:prependCount] + oldMessages
				indexOffset = prependCount
			else:
				if self.isBoundaryScrollPending:
					return False
				newMessages = visibleMessages
				didReplace = True

		state["messages"] = newMessages
		if wasAtLast or didReplace:
			state["currentIndex"] = len(newMessages) - 1
		else:
			state["currentIndex"] = min(oldIndex + indexOffset, len(newMessages) - 1)
		self._trimReviewState(state)
		return didReplace

	def _updateVisibleWindow(
		self,
		state: dict[str, Any],
		records: list[dict[str, Any]],
	):
		"""Store the currently visible message window in queue coordinates."""
		visibleMessages = [record["text"] for record in records]
		visibleStart = self._findSubList(state["messages"], visibleMessages)
		if visibleStart is None:
			state["visibleStart"] = None
			state["visibleEnd"] = None
			return

		state["visibleStart"] = visibleStart
		state["visibleEnd"] = visibleStart + len(visibleMessages) - 1

	def _trimReviewState(self, state: dict[str, Any]):
		"""Keep the temporary review queue within the configured maximum size."""
		overflow = len(state["messages"]) - self.MAX_MESSAGE_QUEUE_SIZE
		if overflow <= 0:
			return
		del state["messages"][:overflow]
		state["currentIndex"] = max(0, state["currentIndex"] - overflow)
		if state["visibleStart"] is not None:
			state["visibleStart"] = max(0, state["visibleStart"] - overflow)
		if state["visibleEnd"] is not None:
			state["visibleEnd"] = max(0, state["visibleEnd"] - overflow)

	def _setNotificationBaseline(
		self,
		state: dict[str, Any],
		records: list[dict[str, Any]],
	):
		"""Mark the newest visible message as already seen."""
		if not records:
			return
		lastRecord = records[-1]
		state["lastReadMessageInfo"] = (lastRecord["info"], lastRecord["text"])

	def refreshMessageQueue(
		self,
		messageList: Any | None = None,
		setNotificationBaseline: bool = False,
	) -> dict[str, Any] | None:
		"""Refresh the temporary queue for the current chat view."""
		self.reviewQueueResetOnLastRefresh = False
		if messageList is None:
			messageList = self._findCurrentMessageList()
		if messageList is None:
			return self.reviewState

		records = self._collectVisibleMessageRecords(messageList)
		if not records:
			return self.reviewState

		state = self.reviewState
		visibleMessages = [record["text"] for record in records]
		self.reviewQueueResetOnLastRefresh = self._mergeVisibleMessages(state, visibleMessages)
		self._updateVisibleWindow(state, records)
		self.activeMessageList = messageList
		if setNotificationBaseline:
			self._setNotificationBaseline(state, records)
		return state

	def _getObjectRect(self, obj: Any) -> tuple[int, int, int, int] | None:
		"""Return an object's screen rectangle."""
		rect = self._getObjectLocation(obj)
		if rect is None:
			return None
		_left, _top, width, height = rect
		if width <= 0 or height <= 0:
			return None
		return rect

	def _getMouseScrollPoint(self, messageList: Any) -> tuple[int, int] | None:
		"""Return the center point inside the message list for wheel scrolling."""
		rect = self._getObjectRect(messageList)
		if rect is None:
			return None
		left, top, width, height = rect
		return int(left + width / 2), int(top + height / 2)

	def _isKeyDown(self, vkCode: int) -> bool:
		"""Return whether a virtual key is currently down."""
		try:
			return bool(winUser.getAsyncKeyState(vkCode) & 32768)
		except Exception:
			return False

	def _getPressedAltKeys(self) -> list[tuple[int, int]]:
		"""Return pressed Alt keys and the flags needed to restore them."""
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
		"""Send synthetic Alt key-up or key-down events for wheel scrolling."""
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

	def _speakReviewMessage(self, text: str):
		"""Speak a review message."""
		ui.message(text)

	def _speakRelativeMessage(self, state: dict[str, Any], direction: int) -> bool:
		"""Speak a message relative to the current review cursor."""
		nextIndex = state["currentIndex"] + direction
		if nextIndex < 0 or nextIndex >= len(state["messages"]):
			return False
		state["currentIndex"] = nextIndex
		self._speakReviewMessage(state["messages"][nextIndex])
		return True

	def _speakMessageAtIndex(self, state: dict[str, Any], index: int) -> bool:
		"""Speak a message at an exact queue index."""
		if index < 0 or index >= len(state["messages"]):
			return False
		state["currentIndex"] = index
		self._speakReviewMessage(state["messages"][index])
		return True

	def _speakCurrentMessage(self, state: dict[str, Any]) -> bool:
		"""Speak the current review message without moving the review cursor."""
		currentIndex = state["currentIndex"]
		if currentIndex < 0 or currentIndex >= len(state["messages"]):
			return False
		self._speakReviewMessage(state["messages"][currentIndex])
		return True

	def _shouldScrollBeforeRelativeRead(self, state: dict[str, Any], direction: int) -> bool:
		"""Return whether the next review step should first move the visible list."""
		nextIndex = state["currentIndex"] + direction
		return nextIndex < 0 or nextIndex >= len(state["messages"])

	def _getBoundaryTargetIndex(
		self,
		state: dict[str, Any],
		oldMessages: list[str],
		direction: int,
	) -> int | None:
		"""Return the exact queue index requested after a boundary scroll."""
		oldMessagesStart = self._findSubList(state["messages"], oldMessages)
		if oldMessagesStart is None:
			return None
		if direction < 0 and oldMessagesStart > 0:
			return oldMessagesStart - 1
		if direction > 0 and oldMessagesStart + len(oldMessages) < len(state["messages"]):
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
			if not state or not state["messages"]:
				return

			targetIndex = self._getBoundaryTargetIndex(state, oldMessages, direction)
			if targetIndex is not None:
				if self._speakMessageAtIndex(state, targetIndex):
					return
			if nextAttempt <= self.MAX_BOUNDARY_SCROLL_ATTEMPTS:
				scheduledRetry = True
				self._scrollBoundaryAndRead(state, direction, attempt=nextAttempt)
				return

			oldMessagesStart = self._findSubList(state["messages"], oldMessages)
			if oldMessagesStart is not None:
				state["currentIndex"] = oldMessagesStart + oldIndex
			return

		finally:
			if not scheduledRetry:
				self.isBoundaryScrollPending = False

	def _getScrollWheelUnitsForAttempt(self, attempt: int) -> int:
		"""Return the number of wheel detents to send for a boundary retry."""
		if attempt >= 2:
			return 8
		if attempt >= 1:
			return 6
		return 4

	def _performScrollAttempt(self, messageList: Any, direction: int, attempt: int) -> bool:
		"""Perform one mouse wheel scroll attempt."""
		wheelUnits = self._getScrollWheelUnitsForAttempt(attempt)
		scrollSteps = winUser.WHEEL_DELTA * wheelUnits
		if direction > 0:
			scrollSteps = -scrollSteps
		if 0 <= attempt <= self.MAX_BOUNDARY_SCROLL_ATTEMPTS:
			return self._scrollMessageList(messageList, scrollSteps)
		return False

	def _scrollBoundaryAndRead(self, state: dict[str, Any], direction: int, attempt: int = 0):
		"""Scroll beyond the visible boundary and read after WeChat updates the list."""
		messageList = self._findCurrentMessageList()
		if messageList is None:
			self.isBoundaryScrollPending = False
			return

		oldMessages = list(state["messages"])
		oldIndex = state["currentIndex"]
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

		The event is used both for new-message notifications and for silently
		keeping the virtual message queue current.
		"""
		try:
			isTargetScrollbar = (
				obj.role == controlTypes.Role.SCROLLBAR
				and obj.parent
				and obj.parent.UIAAutomationId == self.MESSAGE_LIST_UIA_ID
			)
		except Exception:
			return nextHandler()

		if not isTargetScrollbar:
			return nextHandler()

		messageList = obj.parent
		state = self.refreshMessageQueue(messageList)
		if state is None:
			return nextHandler()
		queueWasReset = self.reviewQueueResetOnLastRefresh

		try:
			latestMessage = messageList.lastChild
			latestRecord = self._getMessageRecord(latestMessage)
		except Exception:
			latestRecord = None
		if latestRecord is None:
			return nextHandler()

		currentMessageInfo = (latestRecord["info"], latestRecord["text"])
		if currentMessageInfo == state.get("lastReadMessageInfo"):
			return nextHandler()
		if queueWasReset:
			state["lastReadMessageInfo"] = currentMessageInfo
			return nextHandler()

		if self.notificationMode != self.MODE_OFF and not self.isUserActive:
			self.performNotification(latestMessage)
		state["lastReadMessageInfo"] = currentMessageInfo
		nextHandler()

	def event_gainFocus(self, obj: Any, nextHandler: Any):
		"""
		Handle focus changes in WeChat.

		Entering the message list activates the temporary review queue and marks
		the current newest message as the notification baseline.
		"""
		if obj.role == controlTypes.Role.TOOLBAR and self._getObjectAutomationID(obj) == self.MAIN_TABBAR_UIA_ID:
			wx.CallLater(0, lambda: speech.speakObject(obj.simpleFirstChild))

		try:
			isMessageItem = (
				self._isMessageItem(obj)
				and obj.parent
				and self._isMessageList(obj.parent)
			)
		except Exception:
			isMessageItem = False

		if isMessageItem:
			self.notifyUserActivity()
			self.refreshMessageQueue(obj.parent, setNotificationBaseline=True)
		elif self._isMessageList(obj):
			self.refreshMessageQueue(obj, setNotificationBaseline=True)
		elif self._isMessageInputObject(obj):
			self.refreshMessageQueue(setNotificationBaseline=True)
		nextHandler()

	def notifyUserActivity(self):
		"""Mark the user as active and pause automatic reading briefly."""
		self.isUserActive = True
		if self.activityTimer and self.activityTimer.IsRunning():
			self.activityTimer.Restart(2000)
		else:
			self.activityTimer = wx.CallLater(2000, self._clearUserActivityFlag)

	def _clearUserActivityFlag(self):
		"""Clear the temporary user activity flag."""
		self.isUserActive = False

	def performNotification(self, messageObj: Any):
		"""Play the configured notification for a new message."""
		playWaveFile(self.SOUND_NEW_MESSAGE)
		if self.notificationMode == self.MODE_SOUND_AND_SPEECH:
			ui.message(messageObj.name)

	def _isMessageInputFocus(self) -> bool:
		"""Return whether the current focus is the WeChat chat message input."""
		try:
			focus = api.getFocusObject()
		except Exception:
			return False

		if focus is None:
			return False

		return self._isMessageInputObject(focus)

	def _isMessageInputObject(self, obj: Any) -> bool:
		"""Return whether an object is the WeChat chat message input."""
		return self._getObjectAutomationID(obj) == self.MESSAGE_INPUT_UIA_ID

	def _sendReviewGestureThrough(self, gesture: Any):
		"""Let WeChat or NVDA handle a review gesture outside the message input."""
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
			self.scrollLoadTimer = None
		if self.isBoundaryScrollPending:
			self.isBoundaryScrollPending = False
		try:
			gesture.send()
		except Exception:
			log.debugWarning("Unable to send WeChat review gesture through.", exc_info=True)

	def _readReviewDirection(self, state: dict[str, Any], direction: int) -> bool:
		"""Read one relative review movement, scrolling only at a queue boundary."""
		if direction > 0 and state["currentIndex"] >= len(state["messages"]) - 1:
			return self._speakCurrentMessage(state)
		if self._shouldScrollBeforeRelativeRead(state, direction):
			self._scrollBoundaryAndRead(state, direction)
			return False
		return self._speakRelativeMessage(state, direction)

	def _getCachedReviewState(self) -> dict[str, Any] | None:
		"""Return the temporary review state without refreshing visible messages."""
		state = self.reviewState
		if state is None or not state["messages"]:
			return None
		return state

	def _getActiveReviewState(self, refresh: bool = True) -> dict[str, Any] | None:
		"""Return the active temporary review state."""
		if refresh:
			state = self._getCachedReviewState()
			if state is None:
				state = self.refreshMessageQueue(setNotificationBaseline=True)
		else:
			state = self.reviewState
		if state and state["messages"]:
			return state
		ui.message(self.NO_MESSAGES_TEXT)
		return None

	def _handleRelativeReviewGesture(
		self,
		gesture: Any,
		direction: int,
	):
		"""Handle Alt+Arrow message review from the WeChat message input."""
		if not self._isMessageInputFocus():
			self._sendReviewGestureThrough(gesture)
			return
		if self.isBoundaryScrollPending:
			return
		state = self._getActiveReviewState()
		if state is None:
			return
		self._readReviewDirection(state, direction)

	@script(
		# Translators: Description for the command that reads the previous WeChat message.
		description=_("Reads the previous message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+upArrow",
	)
	def script_readPreviousMessage(self, gesture: Any):
		"""Read the previous message from the active review queue."""
		self._handleRelativeReviewGesture(gesture, -1)

	@script(
		# Translators: Description for the command that reads the next WeChat message.
		description=_("Reads the next message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+downArrow",
	)
	def script_readNextMessage(self, gesture: Any):
		"""Read the next message from the active review queue."""
		self._handleRelativeReviewGesture(gesture, 1)

	@script(
		# Translators: Description for the command that reads the last WeChat message.
		description=_("Reads the last message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+end",
	)
	def script_readLastMessage(self, gesture: Any):
		"""Read the newest message from the active review queue."""
		if not self._isMessageInputFocus():
			self._sendReviewGestureThrough(gesture)
			return
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
			self.scrollLoadTimer = None
		if self.isBoundaryScrollPending:
			self.isBoundaryScrollPending = False
		state = self._getActiveReviewState()
		if state is None:
			return

		state["currentIndex"] = len(state["messages"]) - 1
		self._speakReviewMessage(state["messages"][state["currentIndex"]])

	@script(
		# Translators: Description for the command that changes new message notification mode.
		description=_("Cycles through new message notification modes (Off -> Sound Only -> Sound and Speech)"),
		category=SCRIPT_CATEGORY,
		gesture="kb:f3",
	)
	def script_toggleNotificationMode(self, gesture: Any):
		"""Cycle through new message notification modes."""
		self.notificationMode = (self.notificationMode + 1) % 3
		config.conf[self.CONFIG_SECTION][self.CONFIG_KEY_NOTIFICATION_MODE] = self.notificationMode
		modeMessages = {
			self.MODE_OFF: _("Off"),
			self.MODE_SOUND_ONLY: _("Sound Only"),
			self.MODE_SOUND_AND_SPEECH: _("Sound and speech"),
		}
		ui.message(modeMessages.get(self.notificationMode))
		self.reviewState["lastReadMessageInfo"] = None
