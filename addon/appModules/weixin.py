# -*- coding: utf-8 -*-
# PC WeChat add-on for NVDA
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.
# Copyright (C) 2025 Cary-rowen <manchen_0528@outlook.com>

from __future__ import annotations

from collections import deque
from os.path import dirname, join
from typing import Any

import addonHandler
import api
import appModuleHandler
import config
import controlTypes
import core
import eventHandler
import mouseHandler
import speech
import synthDriverHandler
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
	MAIN_TABBAR_UIA_ID = "main_tabbar"
	MAIN_WINDOW_CLASS_NAME = "Qt51514QWindowToolSaveBits"
	CONFIG_SECTION = "weixin"
	CONFIG_KEY_NOTIFICATION_MODE = "notificationMode"
	MAX_MESSAGE_QUEUE_SIZE = 200
	MAX_DESCENDANTS_TO_SEARCH = 300
	SCROLL_LOAD_DELAY = 100
	MAX_BOUNDARY_SCROLL_ATTEMPTS = 8
	PREFETCH_TARGET_REMAINING = 24
	PREFETCH_VISIBLE_START_LIMIT = 1
	PREFETCH_WHEEL_UNITS = 8
	PREFETCH_LOAD_DELAY = 70
	MAX_CHAINED_PREFETCHES = 4
	PENDING_REVIEW_DRAIN_DELAY = 20
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
		self.chatStates: dict[Any, dict[str, Any]] = {}
		self.activeChatKey = None
		self.activeMessageList = None
		self.nextFallbackChatID = 0
		self.scrollLoadTimer = None
		self.isBoundaryScrollPending = False
		self.prefetchLoadTimer = None
		self.isPrefetchPending = False
		self.isPrefetchRefresh = False
		self.prefetchContext: dict[str, Any] | None = None
		self.pendingReviewDirection: int | None = None
		self.pendingReviewDrainTimer = None
		self.deferredPrefetchContext: dict[str, Any] | None = None
		self.isPendingReviewDrainDeferred = False
		self.isReviewSpeechActive = False
		self.lastQueuedSpeechIndex = 0
		self.lastReachedSpeechIndex = 0
		synthDriverHandler.pre_synthSpeak.register(self._onPreSynthSpeak)
		synthDriverHandler.synthIndexReached.register(self._onSynthIndexReached)
		synthDriverHandler.synthDoneSpeaking.register(self._onSynthDoneSpeaking)

		eventHandler.requestEvents(
			"gainFocus",
			processId=self.processID,
			windowClassName=self.MAIN_WINDOW_CLASS_NAME,
		)

	def terminate(self):
		"""Stop pending timers before unloading the app module."""
		self._cancelMessagePrefetch()
		self._clearPendingReviewGesture()
		self._clearDeferredReviewWork()
		synthDriverHandler.pre_synthSpeak.unregister(self._onPreSynthSpeak)
		synthDriverHandler.synthIndexReached.unregister(self._onSynthIndexReached)
		synthDriverHandler.synthDoneSpeaking.unregister(self._onSynthDoneSpeaking)
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
		self.scrollLoadTimer = None
		if self.activityTimer and self.activityTimer.IsRunning():
			self.activityTimer.Stop()
		self.activityTimer = None
		super().terminate()

	def _getObjectRuntimeID(self, obj: Any) -> tuple[Any, ...] | None:
		"""Return an object's UIA runtime ID when available."""
		try:
			uiaElement = obj.UIAElement
		except Exception:
			return None

		for methodName in ("getRuntimeId", "GetRuntimeId"):
			try:
				return tuple(getattr(uiaElement, methodName)())
			except Exception:
				continue
		return None

	def _getObjectText(self, obj: Any) -> str | None:
		"""Return useful accessible text for an object."""
		try:
			text = obj.name
		except Exception:
			return None
		if not text or speech.isBlank(text):
			return None
		return text

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

	def _getMessageRecord(self, obj: Any) -> dict[str, Any] | None:
		"""Return a stable record for a message object."""
		text = self._getObjectText(obj)
		if text is None:
			return None

		runtimeID = self._getObjectRuntimeID(obj)
		if runtimeID is not None:
			messageInfo = ("runtimeID", runtimeID)
		else:
			try:
				location = obj.location
			except Exception:
				location = None
			try:
				location = tuple(location) if location is not None else None
			except TypeError:
				location = repr(location)
			messageInfo = ("fallback", location, text)

		return {
			"info": messageInfo,
			"text": text,
		}

	def _isMessageList(self, obj: Any) -> bool:
		"""Return whether an object is the WeChat chat message list."""
		try:
			return obj.UIAAutomationId == self.MESSAGE_LIST_UIA_ID
		except Exception:
			return False

	def _getContainingMessageList(self, obj: Any) -> Any | None:
		"""Return the chat message list containing an object."""
		while obj:
			if self._isMessageList(obj):
				return obj
			try:
				obj = obj.parent
			except Exception:
				return None
		return None

	def _findMessageListInTree(self, root: Any) -> Any | None:
		"""Find the current chat message list in an object tree."""
		if root is None:
			return None

		queue = deque([root])
		inspectedCount = 0
		while queue and inspectedCount < self.MAX_DESCENDANTS_TO_SEARCH:
			obj = queue.popleft()
			inspectedCount += 1
			if self._isMessageList(obj):
				return obj
			try:
				queue.extend(obj.children)
			except Exception:
				continue
		return None

	def _findCurrentMessageList(self) -> Any | None:
		"""Return the best current chat message list candidate."""
		try:
			messageList = self._getContainingMessageList(api.getFocusObject())
		except Exception:
			messageList = None
		if messageList is not None:
			return messageList

		if self.activeMessageList is not None and self._isMessageList(self.activeMessageList):
			return self.activeMessageList

		try:
			messageList = self._findMessageListInTree(api.getForegroundObject())
		except Exception:
			messageList = None
		if messageList is not None:
			return messageList

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

		text = self._getCachedUIAProperty(element, UIA.UIA_NamePropertyId)
		if not text or speech.isBlank(text):
			return None

		location = self._getUIARectLocation(
			self._getCachedUIAProperty(element, UIA.UIA_BoundingRectanglePropertyId),
		)
		automationID = self._getCachedUIAProperty(element, UIA.UIA_AutomationIdPropertyId)
		return {
			"info": ("uia", automationID, location, text),
			"text": text,
		}

	def _collectVisibleMessageRecordsWithUIA(
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
			childrenCacheRequest.addProperty(UIA.UIA_BoundingRectanglePropertyId)
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

	def _collectVisibleMessageRecords(
		self,
		messageList: Any,
	) -> list[dict[str, Any]]:
		"""Collect visible message records from the chat message list."""
		records = self._collectVisibleMessageRecordsWithUIA(messageList)
		if records:
			return records

		records = []
		try:
			children = messageList.children
		except Exception:
			children = []

		for child in children:
			try:
				if child.role != controlTypes.Role.LISTITEM:
					continue
			except Exception:
				continue
			record = self._getMessageRecord(child)
			if record is not None:
				records.append(record)

		if records:
			return records

		try:
			record = self._getMessageRecord(messageList.lastChild)
		except Exception:
			record = None
		records = [record] if record is not None else []
		return records

	def _createChatState(self) -> dict[str, Any]:
		"""Create message review state for one chat."""
		return {
			"messages": [],
			"currentIndex": -1,
			"lastReadMessageInfo": None,
			"visibleStart": None,
			"visibleEnd": None,
		}

	def _hasReliableOverlap(self, left: list[str], right: list[str]) -> bool:
		"""Return whether two message windows likely belong to the same chat."""
		if not left or not right:
			return False
		if self._findSubList(left, right) is not None:
			return True
		if self._findSubList(right, left) is not None:
			return True
		commonMessages = set(left).intersection(right)
		return len(commonMessages) >= 2

	def _isScrollMergeProtected(self) -> bool:
		"""Return whether queue refreshes must stay attached to the active chat."""
		return self.isBoundaryScrollPending or self.isPrefetchRefresh

	def _getFallbackChatKey(
		self,
		listRuntimeID: tuple[Any, ...] | None,
		messages: list[str],
	) -> Any:
		"""Return a generated chat key when WeChat does not expose the selected conversation."""
		if self._isScrollMergeProtected() and self.activeChatKey is not None:
			return self.activeChatKey

		activeState = self.chatStates.get(self.activeChatKey)
		if activeState and self._hasReliableOverlap(activeState["messages"], messages):
			return self.activeChatKey

		for chatKey, state in self.chatStates.items():
			if self._hasReliableOverlap(state["messages"], messages):
				return chatKey

		self.nextFallbackChatID += 1
		return ("fallbackChat", self.nextFallbackChatID, listRuntimeID)

	def _getChatState(
		self,
		messageList: Any,
		records: list[dict[str, Any]],
	) -> tuple[Any, dict[str, Any]]:
		"""Return the state belonging to the current chat."""
		messages = [record["text"] for record in records]
		listRuntimeID = self._getObjectRuntimeID(messageList)
		activeState = self.chatStates.get(self.activeChatKey)
		if self._isScrollMergeProtected() and activeState is not None:
			return self.activeChatKey, activeState

		if activeState and self._hasReliableOverlap(activeState["messages"], messages):
			return self.activeChatKey, activeState

		chatKey = self._getFallbackChatKey(listRuntimeID, messages)

		state = self.chatStates.get(chatKey)
		if state is None:
			state = self._createChatState()
			self.chatStates[chatKey] = state
		return chatKey, state

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
	):
		"""Merge currently visible messages into a chat's virtual queue."""
		oldMessages = state["messages"]
		if not oldMessages:
			state["messages"] = visibleMessages
			state["currentIndex"] = len(visibleMessages) - 1
			self._trimChatState(state)
			return

		oldIndex = state["currentIndex"]
		wasAtLast = oldIndex >= len(oldMessages) - 1
		newMessages = None
		indexOffset = 0
		didReplace = False

		visibleStart = self._findSubList(oldMessages, visibleMessages)
		if visibleStart is not None:
			return

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
				if self._isScrollMergeProtected():
					return
				newMessages = visibleMessages
				didReplace = True

		state["messages"] = newMessages
		if wasAtLast or didReplace:
			state["currentIndex"] = len(newMessages) - 1
		else:
			state["currentIndex"] = min(oldIndex + indexOffset, len(newMessages) - 1)
		self._trimChatState(state)

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

	def _trimChatState(self, state: dict[str, Any]):
		"""Keep a chat queue within the configured maximum size."""
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
		"""Refresh and activate the queue for the current chat."""
		if messageList is None:
			messageList = self._findCurrentMessageList()
		if messageList is None:
			return self.chatStates.get(self.activeChatKey)

		records = self._collectVisibleMessageRecords(messageList)
		if not records:
			return self.chatStates.get(self.activeChatKey)

		chatKey, state = self._getChatState(messageList, records)
		visibleMessages = [record["text"] for record in records]
		self._mergeVisibleMessages(state, visibleMessages)
		self._updateVisibleWindow(state, records)
		self.activeChatKey = chatKey
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

	def _getMouseScrollPoints(self, messageList: Any) -> list[tuple[int, int]]:
		"""Return candidate points inside the message list for wheel scrolling."""
		rect = self._getObjectRect(messageList)
		if rect is None:
			return []
		left, top, width, height = rect
		y = int(top + height / 2)
		points = [
			(int(left + width / 2), y),
		]
		return points

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
		points = self._getMouseScrollPoints(messageList)
		if not points:
			return False

		oldX, oldY = winUser.getCursorPos()
		pressedAltKeys = self._getPressedAltKeys()
		try:
			if pressedAltKeys:
				self._setSyntheticAltKeysUp(pressedAltKeys, True)
			for point in points:
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

	def _stopMessagePrefetchTimer(self):
		"""Stop the pending message prefetch timer."""
		if self.prefetchLoadTimer and self.prefetchLoadTimer.IsRunning():
			self.prefetchLoadTimer.Stop()
		self.prefetchLoadTimer = None

	def _clearMessagePrefetchState(self):
		"""Clear message prefetch state without moving the UI."""
		self.isPrefetchPending = False
		self.isPrefetchRefresh = False
		self.prefetchContext = None

	def _cancelMessagePrefetch(self):
		"""Cancel any pending message prefetch."""
		self._stopMessagePrefetchTimer()
		self._clearMessagePrefetchState()

	def _clearDeferredReviewWork(self):
		"""Clear review work that is waiting for current speech to finish."""
		self.deferredPrefetchContext = None
		self.isPendingReviewDrainDeferred = False

	def _isSpeechActive(self) -> bool:
		"""Return whether NVDA appears to still be speaking."""
		return self.isReviewSpeechActive or self.lastQueuedSpeechIndex != self.lastReachedSpeechIndex

	def _onPreSynthSpeak(self, speechSequence: speech.SpeechSequence):
		"""Track the latest speech index sent to the synthesizer."""
		try:
			index = speechSequence[-1].index
		except Exception:
			return
		self.lastQueuedSpeechIndex = index

	def _onSynthIndexReached(self, synth: synthDriverHandler.SynthDriver, index: int):
		"""Track the latest speech index reached by the synthesizer."""
		self.lastReachedSpeechIndex = index
		if self.deferredPrefetchContext is not None or self.isPendingReviewDrainDeferred:
			core.callLater(0, self._runDeferredReviewWork)

	def _onSynthDoneSpeaking(self):
		"""Run deferred review work after NVDA finishes speaking."""
		self.isReviewSpeechActive = False
		if self.deferredPrefetchContext is None and not self.isPendingReviewDrainDeferred:
			return
		core.callLater(0, self._runDeferredReviewWork)

	def _speakReviewMessage(self, text: str):
		"""Speak a review message and mark speech-sensitive work as blocked."""
		self.isReviewSpeechActive = True
		ui.message(text)

	def _deferMessagePrefetchUntilSpeechDone(
		self,
		state: dict[str, Any],
		direction: int,
		chainCount: int = 0,
	):
		"""Start prefetch only after the boundary message has finished speaking."""
		if direction >= 0:
			return
		self.deferredPrefetchContext = {
			"chainCount": chainCount,
			"chatKey": self.activeChatKey,
			"direction": direction,
			"state": state,
		}

	def _deferPendingReviewDrainUntilSpeechDone(self):
		"""Read queued review gestures after the boundary message has finished speaking."""
		if self.pendingReviewDirection is None:
			return
		self.isPendingReviewDrainDeferred = True

	def _runDeferredReviewWork(self):
		"""Run pending review or prefetch work once speech has completed."""
		if self._isSpeechActive():
			return
		if self.isBoundaryScrollPending:
			return
		if not self._isMessageInputFocus():
			self._clearDeferredReviewWork()
			self._clearPendingReviewGesture()
			return
		if self.isPendingReviewDrainDeferred and self.pendingReviewDirection is not None:
			self.isPendingReviewDrainDeferred = False
			self._schedulePendingReviewDrain()
			return

		context = self.deferredPrefetchContext
		self.deferredPrefetchContext = None
		if context is None:
			return
		if context.get("chatKey") != self.activeChatKey:
			return
		if self.pendingReviewDirection is not None:
			return
		self._maybeStartMessagePrefetch(
			context["state"],
			context["direction"],
			context.get("chainCount", 0),
		)

	def _scheduleMessagePrefetchFinish(self):
		"""Schedule a quiet refresh to finish the active message prefetch."""
		if not self.isPrefetchPending or self.prefetchContext is None:
			return
		self._stopMessagePrefetchTimer()
		self.prefetchLoadTimer = wx.CallLater(
			self.PREFETCH_LOAD_DELAY,
			self._finishMessagePrefetch,
			"timer",
		)

	def _restorePrefetchReviewPosition(
		self,
		state: dict[str, Any],
		oldMessages: list[str],
		oldIndex: int,
	) -> bool:
		"""Restore the review cursor after a verified prefetch merge."""
		oldMessagesStart = self._findSubList(state["messages"], oldMessages)
		if oldMessagesStart is not None and 0 <= oldIndex < len(oldMessages):
			state["currentIndex"] = min(oldMessagesStart + oldIndex, len(state["messages"]) - 1)
			return True
		return self._restoreReviewPosition(state, oldMessages, oldIndex)

	def _performMessagePrefetchScroll(self, context: dict[str, Any]) -> bool:
		"""Perform one background prefetch scroll."""
		messageList = context.get("messageList")
		if messageList is None:
			return False
		scrollSteps = winUser.WHEEL_DELTA * self.PREFETCH_WHEEL_UNITS
		return self._scrollMessageList(messageList, scrollSteps)

	def _shouldContinueMessagePrefetch(self, state: dict[str, Any], chainCount: int) -> bool:
		"""Return whether another background prefetch page should be requested."""
		if chainCount >= self.MAX_CHAINED_PREFETCHES:
			return False
		currentIndex = state.get("currentIndex", -1)
		visibleStart = state.get("visibleStart")
		return (
			0 <= currentIndex < self.PREFETCH_TARGET_REMAINING
			and visibleStart is not None
			and visibleStart <= self.PREFETCH_VISIBLE_START_LIMIT
		)

	def _scheduleChainedMessagePrefetch(self, state: dict[str, Any], chainCount: int):
		"""Start the next prefetch page after the current merge settles."""
		wx.CallLater(
			1,
			self._maybeStartMessagePrefetch,
			state,
			-1,
			chainCount,
		)

	def _finishMessagePrefetch(self, reason: str = "timer") -> dict[str, Any] | None:
		"""Merge one completed message prefetch when ordering is verified."""
		context = self.prefetchContext
		if not self.isPrefetchPending or context is None:
			return None
		if reason == "timer":
			self.prefetchLoadTimer = None
		if context.get("chatKey") != self.activeChatKey:
			self._cancelMessagePrefetch()
			return None

		oldMessages = context["oldMessages"]
		oldIndex = context["oldIndex"]
		self.isPrefetchRefresh = True
		try:
			state = self.refreshMessageQueue(setNotificationBaseline=True)
		finally:
			self.isPrefetchRefresh = False
		if not state or not state["messages"]:
			self._stopMessagePrefetchTimer()
			self._clearMessagePrefetchState()
			return state

		self._restorePrefetchReviewPosition(state, oldMessages, oldIndex)
		oldMessagesStart = self._findSubList(state["messages"], oldMessages)
		if oldMessagesStart is None or oldMessagesStart <= 0:
			self._stopMessagePrefetchTimer()
			self._clearMessagePrefetchState()
			return state

		chainCount = context.get("chainCount", 0) + 1
		self._stopMessagePrefetchTimer()
		self._clearMessagePrefetchState()
		if reason == "timer" and self._shouldContinueMessagePrefetch(state, chainCount):
			self._scheduleChainedMessagePrefetch(state, chainCount)
		return state

	def _maybeStartMessagePrefetch(
		self,
		state: dict[str, Any],
		direction: int,
		chainCount: int = 0,
	):
		"""Start a quiet older-message prefetch after a verified boundary read."""
		if direction >= 0:
			return
		if self._isSpeechActive():
			self._deferMessagePrefetchUntilSpeechDone(state, direction, chainCount)
			return
		if self.isBoundaryScrollPending or self.isPrefetchPending:
			return
		if self.activeChatKey is None or not state.get("messages"):
			return
		currentIndex = state["currentIndex"]
		if currentIndex < 0:
			return
		if currentIndex >= self.PREFETCH_TARGET_REMAINING:
			return
		visibleStart = state.get("visibleStart")
		if visibleStart is None or visibleStart > self.PREFETCH_VISIBLE_START_LIMIT:
			return

		messageList = self._findCurrentMessageList()
		if messageList is None:
			return

		self.prefetchContext = {
			"chatKey": self.activeChatKey,
			"chainCount": chainCount,
			"messageList": messageList,
			"oldIndex": currentIndex,
			"oldMessages": list(state["messages"]),
		}
		self.isPrefetchPending = True
		if not self._performMessagePrefetchScroll(self.prefetchContext):
			self._cancelMessagePrefetch()
			return
		self._scheduleMessagePrefetchFinish()

	def _restoreReviewPosition(
		self,
		state: dict[str, Any],
		oldMessages: list[str],
		oldIndex: int,
	) -> bool:
		"""Restore the review cursor to the same message after queue refresh."""
		messages = state["messages"]
		if not messages or not oldMessages or oldIndex < 0 or oldIndex >= len(oldMessages):
			return False

		anchorText = oldMessages[oldIndex]
		previousText = oldMessages[oldIndex - 1] if oldIndex > 0 else None
		nextText = oldMessages[oldIndex + 1] if oldIndex < len(oldMessages) - 1 else None
		bestIndex = None
		bestScore = -1

		for index, message in enumerate(messages):
			if message != anchorText:
				continue
			score = 0
			if previousText is not None and index > 0 and messages[index - 1] == previousText:
				score += 2
			if nextText is not None and index < len(messages) - 1 and messages[index + 1] == nextText:
				score += 2
			if score > bestScore:
				bestIndex = index
				bestScore = score

		if bestIndex is not None:
			state["currentIndex"] = bestIndex
			return True
		return False

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

	def _isBoundaryTargetVisible(
		self,
		state: dict[str, Any],
		targetIndex: int | None,
	) -> bool:
		"""Return whether a boundary scroll exposed the next review target."""
		visibleStart = state.get("visibleStart")
		visibleEnd = state.get("visibleEnd")
		if targetIndex is None or visibleStart is None or visibleEnd is None:
			return False
		return visibleStart <= targetIndex <= visibleEnd

	def _getBoundaryTargetIndex(
		self,
		state: dict[str, Any],
		oldMessages: list[str],
		oldIndex: int,
		direction: int,
	) -> int | None:
		"""Return the exact queue index requested after a boundary scroll."""
		oldMessagesStart = self._findSubList(state["messages"], oldMessages)
		targetOldIndex = oldIndex + direction
		if oldMessagesStart is not None and 0 <= targetOldIndex < len(oldMessages):
			return oldMessagesStart + targetOldIndex
		if oldMessagesStart is not None and direction < 0 and targetOldIndex < 0:
			return oldMessagesStart - 1
		if oldMessagesStart is not None and direction > 0 and targetOldIndex >= len(oldMessages):
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
		prefetchState = None
		try:
			if not self._isMessageInputFocus():
				self._clearPendingReviewGesture()
				return
			state = self.refreshMessageQueue(setNotificationBaseline=True)
			if not state or not state["messages"]:
				return

			targetIndex = self._getBoundaryTargetIndex(state, oldMessages, oldIndex, direction)
			targetWasCached = 0 <= oldIndex + direction < len(oldMessages)
			targetVisible = self._isBoundaryTargetVisible(state, targetIndex)
			if targetIndex is not None and (targetWasCached or targetVisible):
				if self._speakMessageAtIndex(state, targetIndex):
					prefetchState = state
					return
			if targetWasCached:
				restored = self._restoreReviewPosition(state, oldMessages, oldIndex)
				if not restored:
					oldMessagesStart = self._findSubList(state["messages"], oldMessages)
					if oldMessagesStart is not None:
						state["currentIndex"] = oldMessagesStart + oldIndex
					else:
						state["currentIndex"] = min(oldIndex, len(state["messages"]) - 1)
				if self._speakRelativeMessage(state, direction):
					prefetchState = state
					return
				return
			if not targetVisible and nextAttempt <= self.MAX_BOUNDARY_SCROLL_ATTEMPTS:
				scheduledRetry = True
				self._scrollBoundaryAndRead(state, direction, attempt=nextAttempt)
				return

			if not targetVisible and not targetWasCached:
				restored = self._restoreReviewPosition(state, oldMessages, oldIndex)
				if not restored:
					oldMessagesStart = self._findSubList(state["messages"], oldMessages)
					if oldMessagesStart is not None:
						state["currentIndex"] = oldMessagesStart + oldIndex
				return

		finally:
			if not scheduledRetry:
				self.isBoundaryScrollPending = False
				if prefetchState is not None:
					if self.pendingReviewDirection is not None:
						self._deferPendingReviewDrainUntilSpeechDone()
					else:
						self._deferMessagePrefetchUntilSpeechDone(prefetchState, direction)
				elif self.pendingReviewDirection is not None:
					self._schedulePendingReviewDrain()

	def _getScrollWheelUnitsForAttempt(self, attempt: int) -> int:
		"""Return the number of wheel detents to send for a boundary retry."""
		if attempt >= 4:
			return 16
		if attempt >= 2:
			return 12
		return 8

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
		if self.isPrefetchPending:
			self._cancelMessagePrefetch()
		messageList = self._findCurrentMessageList()
		if messageList is None:
			self.isBoundaryScrollPending = False
			self._clearPendingReviewGesture()
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
			self._clearPendingReviewGesture()
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

		if self.isPrefetchPending:
			return nextHandler()

		previousChatKey = self.activeChatKey
		messageList = obj.parent
		state = self.refreshMessageQueue(messageList)
		if state is None:
			return nextHandler()

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
		if previousChatKey != self.activeChatKey:
			state["lastReadMessageInfo"] = currentMessageInfo
			return nextHandler()

		if self.notificationMode != self.MODE_OFF and not self.isUserActive:
			self.performNotification(latestMessage)
		state["lastReadMessageInfo"] = currentMessageInfo
		nextHandler()

	def event_gainFocus(self, obj: Any, nextHandler: Any):
		"""
		Handle focus changes in WeChat.

		Entering the message list activates the matching chat queue and marks
		the current newest message as the notification baseline.
		"""
		if obj.role == controlTypes.Role.TOOLBAR and obj.UIAAutomationId == self.MAIN_TABBAR_UIA_ID:
			wx.CallLater(0, lambda: speech.speakObject(obj.simpleFirstChild))

		try:
			isMessageItem = (
				obj.role == controlTypes.Role.LISTITEM
				and obj.parent
				and obj.parent.UIAAutomationId == self.MESSAGE_LIST_UIA_ID
			)
		except Exception:
			isMessageItem = False

		if isMessageItem:
			self._cancelMessagePrefetch()
			self.notifyUserActivity()
			self.refreshMessageQueue(obj.parent, setNotificationBaseline=True)
		elif self._isMessageList(obj):
			self._cancelMessagePrefetch()
			self.refreshMessageQueue(obj, setNotificationBaseline=True)
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

		try:
			return focus.UIAAutomationId == self.MESSAGE_INPUT_UIA_ID
		except Exception:
			return False

	def _sendReviewGestureThrough(self, gesture: Any):
		"""Let WeChat or NVDA handle a review gesture outside the message input."""
		self._clearPendingReviewGesture()
		self._clearDeferredReviewWork()
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
			self.scrollLoadTimer = None
		if self.isBoundaryScrollPending:
			self.isBoundaryScrollPending = False
		try:
			gesture.send()
		except Exception:
			log.debugWarning("Unable to send WeChat review gesture through.", exc_info=True)

	def _clearPendingReviewGesture(self):
		"""Clear the queued review gesture and stop its drain timer."""
		self.pendingReviewDirection = None
		if self.pendingReviewDrainTimer and self.pendingReviewDrainTimer.IsRunning():
			self.pendingReviewDrainTimer.Stop()
		self.pendingReviewDrainTimer = None

	def _queuePendingReviewGesture(self, direction: int):
		"""Queue a review movement while a boundary scroll is loading."""
		self.pendingReviewDirection = direction

	def _schedulePendingReviewDrain(self):
		"""Schedule the queued review gesture to run after a boundary load speaks."""
		if self.pendingReviewDirection is None:
			return
		if self.pendingReviewDrainTimer and self.pendingReviewDrainTimer.IsRunning():
			return
		self.pendingReviewDrainTimer = wx.CallLater(
			self.PENDING_REVIEW_DRAIN_DELAY,
			self._drainPendingReviewGesture,
		)

	def _readReviewDirection(self, state: dict[str, Any], direction: int) -> bool:
		"""Read one relative review movement, scrolling only at a queue boundary."""
		if direction > 0 and state["currentIndex"] >= len(state["messages"]) - 1:
			return self._speakCurrentMessage(state)
		if self._shouldScrollBeforeRelativeRead(state, direction):
			self._scrollBoundaryAndRead(state, direction)
			return False
		return self._speakRelativeMessage(state, direction)

	def _drainPendingReviewGesture(self):
		"""Read the queued review gesture after the boundary message is available."""
		self.pendingReviewDrainTimer = None
		if self.pendingReviewDirection is None:
			return
		if self.isBoundaryScrollPending:
			return
		if not self._isMessageInputFocus():
			self._clearPendingReviewGesture()
			return

		prefetchedState = None
		if self.isPrefetchPending:
			prefetchedState = self._finishMessagePrefetch("pendingReview")
		state = prefetchedState or self._getActiveChatState(refresh=False)
		if state is None:
			self._clearPendingReviewGesture()
			return

		direction = self.pendingReviewDirection
		self.pendingReviewDirection = None
		didSpeak = self._readReviewDirection(state, direction)
		if self.isBoundaryScrollPending:
			return
		if not didSpeak:
			self._clearPendingReviewGesture()
			return
		if direction < 0:
			self._deferMessagePrefetchUntilSpeechDone(state, direction)

	def _getActiveChatState(self, refresh: bool = True) -> dict[str, Any] | None:
		"""Return the refreshed state for the active chat."""
		if refresh:
			state = self.refreshMessageQueue(setNotificationBaseline=True)
		else:
			state = self.chatStates.get(self.activeChatKey)
		if state and state["messages"]:
			return state
		ui.message(self.NO_MESSAGES_TEXT)
		return None

	def _handleRelativeReviewGesture(
		self,
		gesture: Any,
		direction: int,
		prefetchFinishReason: str,
	):
		"""Handle Alt+Arrow message review from the WeChat message input."""
		if not self._isMessageInputFocus():
			self._sendReviewGestureThrough(gesture)
			return
		if self.isBoundaryScrollPending:
			self._queuePendingReviewGesture(direction)
			return
		self._clearPendingReviewGesture()
		self._clearDeferredReviewWork()
		prefetchedState = None
		if self.isPrefetchPending:
			prefetchedState = self._finishMessagePrefetch(prefetchFinishReason)
		state = prefetchedState or self._getActiveChatState(refresh=not self.isPrefetchPending)
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
		"""Read the previous message from the active chat queue."""
		self._handleRelativeReviewGesture(gesture, -1, "readPrevious")

	@script(
		# Translators: Description for the command that reads the next WeChat message.
		description=_("Reads the next message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+downArrow",
	)
	def script_readNextMessage(self, gesture: Any):
		"""Read the next message from the active chat queue."""
		self._handleRelativeReviewGesture(gesture, 1, "readNext")

	@script(
		# Translators: Description for the command that reads the last WeChat message.
		description=_("Reads the last message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+end",
	)
	def script_readLastMessage(self, gesture: Any):
		"""Read the newest message from the active chat queue."""
		if not self._isMessageInputFocus():
			self._sendReviewGestureThrough(gesture)
			return
		self._clearPendingReviewGesture()
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
			self.scrollLoadTimer = None
		if self.isBoundaryScrollPending:
			self.isBoundaryScrollPending = False
		self._clearDeferredReviewWork()
		if self.isPrefetchPending:
			self._cancelMessagePrefetch()
		state = self._getActiveChatState()
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
		for state in self.chatStates.values():
			state["lastReadMessageInfo"] = None
