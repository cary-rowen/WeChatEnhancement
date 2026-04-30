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
	MAX_PENDING_REVIEW_GESTURES = 1
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
		self.pendingReviewDirections: list[int] = []
		self.pendingReviewDrainTimer = None
		self.deferredPrefetchContext: dict[str, Any] | None = None
		self.isPendingReviewDrainDeferred = False
		self.isReviewSpeechActive = False
		self.newSpeechIndex = 0
		self.currentSpeechIndex = 0
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
		self._cancelMessagePrefetch("terminate")
		self._clearPendingReviewGestures("terminate")
		self._clearDeferredReviewWork("terminate")
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

	def _shortenForLog(self, text: Any, limit: int = 80) -> str:
		"""Return a compact text representation for diagnostic logs."""
		if text is None:
			return "<None>"
		text = str(text).replace("\r", "\\r").replace("\n", "\\n")
		if len(text) <= limit:
			return text
		return f"{text[:limit]}..."

	def _getObjectLocation(self, obj: Any) -> tuple[int, int, int, int] | None:
		"""Return an object's location without emitting diagnostic logs."""
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

	def _logQueueSnapshot(self, prefix: str, messages: list[str]):
		"""Log a compact queue snapshot for diagnostics."""
		if not messages:
			log.io(f"WeChatEnhancement: {prefix}: count=0")
			return
		log.io(
			"WeChatEnhancement: "
			f"{prefix}: count={len(messages)}, "
			f"first={self._shortenForLog(messages[0])!r}, "
			f"last={self._shortenForLog(messages[-1])!r}",
		)

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
			"location": self._getObjectLocation(obj),
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

	def _findMessageListInTree(self, root: Any, logDetails: bool = True) -> Any | None:
		"""Find the current chat message list in an object tree."""
		if root is None:
			if logDetails:
				log.io("WeChatEnhancement: message list search skipped: root is None")
			return None

		queue = deque([root])
		inspectedCount = 0
		while queue and inspectedCount < self.MAX_DESCENDANTS_TO_SEARCH:
			obj = queue.popleft()
			inspectedCount += 1
			if self._isMessageList(obj):
				if logDetails:
					log.io(
						"WeChatEnhancement: "
						f"message list found in tree after {inspectedCount} objects",
					)
				return obj
			try:
				queue.extend(obj.children)
			except Exception:
				continue
		if logDetails:
			log.io(
				"WeChatEnhancement: "
				f"message list not found in tree after {inspectedCount} objects",
			)
		return None

	def _findCurrentMessageList(self, logDetails: bool = True) -> Any | None:
		"""Return the best current chat message list candidate."""
		try:
			messageList = self._getContainingMessageList(api.getFocusObject())
		except Exception:
			messageList = None
		if messageList is not None:
			if logDetails:
				log.io("WeChatEnhancement: current message list found from focus ancestry")
			return messageList

		if self.activeMessageList is not None and self._isMessageList(self.activeMessageList):
			if logDetails:
				log.io("WeChatEnhancement: current message list using cached active list")
			return self.activeMessageList

		try:
			messageList = self._findMessageListInTree(api.getForegroundObject(), logDetails=logDetails)
		except Exception:
			messageList = None
		if messageList is not None:
			if logDetails:
				log.io("WeChatEnhancement: current message list found from foreground tree")
			return messageList

		if logDetails:
			log.io("WeChatEnhancement: no current message list found")
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
			"location": location,
			"text": text,
		}

	def _collectVisibleMessageRecordsWithUIA(
		self,
		messageList: Any,
		logDetails: bool = True,
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
		except Exception as e:
			if logDetails:
				log.io(f"WeChatEnhancement: direct UIA children failed: {e!r}")
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

		if records and logDetails:
			self._logQueueSnapshot(
				"visible records from cached UIA children",
				[record["text"] for record in records],
			)
		return records

	def _collectVisibleMessageRecords(
		self,
		messageList: Any,
		logDetails: bool = True,
	) -> list[dict[str, Any]]:
		"""Collect visible message records from the chat message list."""
		records = self._collectVisibleMessageRecordsWithUIA(messageList, logDetails=logDetails)
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
			if logDetails:
				self._logQueueSnapshot(
					"visible records from children",
					[record["text"] for record in records],
				)
				log.io(
					"WeChatEnhancement: "
					f"visible record locations={[record.get('location') for record in records]!r}",
				)
			return records

		try:
			record = self._getMessageRecord(messageList.lastChild)
		except Exception:
			record = None
		records = [record] if record is not None else []
		if logDetails:
			self._logQueueSnapshot(
				"visible records from lastChild fallback",
				[record["text"] for record in records],
			)
		return records

	def _createChatState(self, listRuntimeID: tuple[Any, ...] | None) -> dict[str, Any]:
		"""Create message review state for one chat."""
		return {
			"listRuntimeID": listRuntimeID,
			"messages": [],
			"currentIndex": -1,
			"lastReadMessageInfo": None,
			"visibleMessages": [],
			"visibleSignature": (),
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
		logDetails: bool = True,
	) -> Any:
		"""Return a generated chat key when WeChat does not expose the selected conversation."""
		if self._isScrollMergeProtected() and self.activeChatKey is not None:
			if logDetails:
				log.io(
					"WeChatEnhancement: "
					f"fallback chat key kept during protected scroll={self.activeChatKey!r}",
				)
			return self.activeChatKey

		activeState = self.chatStates.get(self.activeChatKey)
		if activeState and self._hasReliableOverlap(activeState["messages"], messages):
			if logDetails:
				log.io(
					"WeChatEnhancement: "
					f"fallback chat key reused active key={self.activeChatKey!r}",
				)
			return self.activeChatKey

		for chatKey, state in self.chatStates.items():
			if self._hasReliableOverlap(state["messages"], messages):
				if logDetails:
					log.io(f"WeChatEnhancement: fallback chat key reused key={chatKey!r}")
				return chatKey

		self.nextFallbackChatID += 1
		chatKey = ("fallbackChat", self.nextFallbackChatID, listRuntimeID)
		if logDetails:
			log.io(f"WeChatEnhancement: fallback chat key created key={chatKey!r}")
		return chatKey

	def _getChatState(
		self,
		messageList: Any,
		records: list[dict[str, Any]],
		logDetails: bool = True,
	) -> tuple[Any, dict[str, Any]]:
		"""Return the state belonging to the current chat."""
		messages = [record["text"] for record in records]
		listRuntimeID = self._getObjectRuntimeID(messageList)
		activeState = self.chatStates.get(self.activeChatKey)
		if self._isScrollMergeProtected() and activeState is not None:
			activeState["listRuntimeID"] = listRuntimeID
			if logDetails:
				log.io(
					"WeChatEnhancement: "
					f"using active chat state during protected scroll key={self.activeChatKey!r}",
				)
			return self.activeChatKey, activeState

		if activeState and self._hasReliableOverlap(activeState["messages"], messages):
			activeState["listRuntimeID"] = listRuntimeID
			if logDetails:
				log.io(
					"WeChatEnhancement: "
					f"using active chat state by reliable overlap key={self.activeChatKey!r}",
				)
			return self.activeChatKey, activeState

		chatKey = self._getFallbackChatKey(listRuntimeID, messages, logDetails=logDetails)

		state = self.chatStates.get(chatKey)
		if state is None:
			state = self._createChatState(listRuntimeID)
			self.chatStates[chatKey] = state
			if logDetails:
				log.io(f"WeChatEnhancement: created chat state for key={chatKey!r}")
		else:
			state["listRuntimeID"] = listRuntimeID
			if logDetails:
				log.io(f"WeChatEnhancement: using existing chat state for key={chatKey!r}")
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
		logDetails: bool = True,
	):
		"""Merge currently visible messages into a chat's virtual queue."""
		oldMessages = state["messages"]
		if logDetails:
			self._logQueueSnapshot("merge old queue", oldMessages)
			self._logQueueSnapshot("merge visible messages", visibleMessages)
		if not oldMessages:
			state["messages"] = visibleMessages
			state["currentIndex"] = len(visibleMessages) - 1
			self._trimChatState(state)
			if logDetails:
				log.io(
					"WeChatEnhancement: "
					f"merge initialized queue, currentIndex={state['currentIndex']}",
				)
			return

		oldIndex = state["currentIndex"]
		wasAtLast = oldIndex >= len(oldMessages) - 1
		newMessages = None
		indexOffset = 0
		didReplace = False

		visibleStart = self._findSubList(oldMessages, visibleMessages)
		if visibleStart is not None:
			if logDetails:
				log.io(
					"WeChatEnhancement: "
					f"merge skipped, visible window already in queue at index={visibleStart}",
				)
			return

		oldStart = self._findSubList(visibleMessages, oldMessages)
		if oldStart is not None:
			newMessages = visibleMessages
			indexOffset = oldStart
			mergeMode = f"replace with visible containing old queue at index={oldStart}"
		else:
			appendOverlap = self._getSuffixPrefixOverlap(oldMessages, visibleMessages)
			prependOverlap = self._getSuffixPrefixOverlap(visibleMessages, oldMessages)
			if appendOverlap >= prependOverlap and appendOverlap > 0:
				newMessages = oldMessages + visibleMessages[appendOverlap:]
				mergeMode = f"append overlap={appendOverlap}"
			elif prependOverlap > 0:
				prependCount = len(visibleMessages) - prependOverlap
				newMessages = visibleMessages[:prependCount] + oldMessages
				indexOffset = prependCount
				mergeMode = f"prepend overlap={prependOverlap}, prependCount={prependCount}"
			else:
				if self._isScrollMergeProtected():
					if logDetails:
						log.io("WeChatEnhancement: merge skipped no overlap during protected scroll")
					return
				newMessages = visibleMessages
				didReplace = True
				mergeMode = "replace no overlap"

		state["messages"] = newMessages
		if wasAtLast or didReplace:
			state["currentIndex"] = len(newMessages) - 1
		else:
			state["currentIndex"] = min(oldIndex + indexOffset, len(newMessages) - 1)
		self._trimChatState(state)
		if logDetails:
			log.io(
				"WeChatEnhancement: "
				f"merge mode={mergeMode}, oldIndex={oldIndex}, "
				f"newIndex={state['currentIndex']}",
			)
			self._logQueueSnapshot("merge result queue", state["messages"])

	def _getVisibleWindowSignature(
		self,
		records: list[dict[str, Any]],
	) -> tuple[tuple[str, tuple[int, int, int, int] | None], ...]:
		"""Return a signature for the current visible message window."""
		return tuple((record["text"], record.get("location")) for record in records)

	def _updateVisibleWindow(
		self,
		state: dict[str, Any],
		records: list[dict[str, Any]],
		logDetails: bool = True,
	):
		"""Store the currently visible message window in queue coordinates."""
		visibleMessages = [record["text"] for record in records]
		state["visibleMessages"] = list(visibleMessages)
		state["visibleSignature"] = self._getVisibleWindowSignature(records)
		visibleStart = self._findSubList(state["messages"], visibleMessages)
		if visibleStart is None:
			state["visibleStart"] = None
			state["visibleEnd"] = None
			if logDetails:
				log.io("WeChatEnhancement: visible window not found in queue")
			return

		state["visibleStart"] = visibleStart
		state["visibleEnd"] = visibleStart + len(visibleMessages) - 1
		if logDetails:
			log.io(
				"WeChatEnhancement: "
				f"visible window range=({state['visibleStart']}, {state['visibleEnd']})",
			)

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
		logDetails: bool = True,
	) -> dict[str, Any] | None:
		"""Refresh and activate the queue for the current chat."""
		if logDetails:
			log.io(
				"WeChatEnhancement: "
				f"refreshMessageQueue start, hasMessageList={messageList is not None}, "
				f"setNotificationBaseline={setNotificationBaseline}",
			)
		if messageList is None:
			messageList = self._findCurrentMessageList(logDetails=logDetails)
		if messageList is None:
			if logDetails:
				log.io(
					"WeChatEnhancement: "
					f"refreshMessageQueue no message list, activeChatKey={self.activeChatKey!r}",
				)
			return self.chatStates.get(self.activeChatKey)

		records = self._collectVisibleMessageRecords(messageList, logDetails=logDetails)
		if not records:
			if logDetails:
				log.io(
					"WeChatEnhancement: "
					f"refreshMessageQueue no records, activeChatKey={self.activeChatKey!r}",
				)
			return self.chatStates.get(self.activeChatKey)

		previousChatKey = self.activeChatKey
		chatKey, state = self._getChatState(messageList, records, logDetails=logDetails)
		visibleMessages = [record["text"] for record in records]
		self._mergeVisibleMessages(state, visibleMessages, logDetails=logDetails)
		self._updateVisibleWindow(state, records, logDetails=logDetails)
		self.activeChatKey = chatKey
		self.activeMessageList = messageList
		if setNotificationBaseline:
			self._setNotificationBaseline(state, records)
		if logDetails:
			log.io(
				"WeChatEnhancement: "
				f"refreshMessageQueue done, previousKey={previousChatKey!r}, "
				f"activeKey={self.activeChatKey!r}, count={len(state['messages'])}, "
				f"currentIndex={state['currentIndex']}",
			)
		return state

	def _getObjectRect(self, obj: Any) -> tuple[int, int, int, int] | None:
		"""Return an object's screen rectangle."""
		try:
			location = obj.location
		except Exception:
			log.io("WeChatEnhancement: object rect failed: no location")
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
				log.io(
					"WeChatEnhancement: "
					f"object rect failed: invalid location={location!r}",
				)
				return None
		rect = (int(left), int(top), int(width), int(height))
		if width <= 0 or height <= 0:
			log.io(f"WeChatEnhancement: object rect failed: non-positive rect={rect!r}")
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
			log.io("WeChatEnhancement: scrollMessageList aborted: no points")
			return False

		oldX, oldY = winUser.getCursorPos()
		pressedAltKeys = self._getPressedAltKeys()
		try:
			if pressedAltKeys:
				log.io(
					"WeChatEnhancement: "
					f"scrollMessageList temporarily releasing Alt for wheel, keys={pressedAltKeys!r}",
				)
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
					log.io("WeChatEnhancement: scrollMessageList restored Alt after wheel")
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

	def _cancelMessagePrefetch(self, reason: str):
		"""Cancel any pending message prefetch."""
		if self.isPrefetchPending or self.prefetchContext is not None:
			log.io(f"WeChatEnhancement: message prefetch cancelled, reason={reason}")
		self._stopMessagePrefetchTimer()
		self._clearMessagePrefetchState()

	def _clearDeferredReviewWork(self, reason: str):
		"""Clear review work that is waiting for current speech to finish."""
		if self.deferredPrefetchContext is not None:
			log.io(f"WeChatEnhancement: deferred message prefetch cleared, reason={reason}")
		if self.isPendingReviewDrainDeferred:
			log.io(f"WeChatEnhancement: deferred pending review drain cleared, reason={reason}")
		self.deferredPrefetchContext = None
		self.isPendingReviewDrainDeferred = False

	def _isSpeechActive(self) -> bool:
		"""Return whether NVDA appears to still be speaking."""
		return self.isReviewSpeechActive or self.newSpeechIndex != self.currentSpeechIndex

	def _onPreSynthSpeak(self, speechSequence: speech.SequenceItemT):
		"""Track the latest speech index sent to the synthesizer."""
		try:
			index = speechSequence[-1].index
		except Exception:
			return
		self.newSpeechIndex = index

	def _onSynthIndexReached(self, synth: synthDriverHandler.SynthDriver, index: int):
		"""Track the latest speech index reached by the synthesizer."""
		self.currentSpeechIndex = index
		if self.deferredPrefetchContext is not None or self.isPendingReviewDrainDeferred:
			core.callLater(0, self._runDeferredReviewWork)

	def _onSynthDoneSpeaking(self):
		"""Run deferred review work after NVDA finishes speaking."""
		self.isReviewSpeechActive = False
		if self.deferredPrefetchContext is None and not self.isPendingReviewDrainDeferred:
			return
		log.io("WeChatEnhancement: review speech done; scheduling deferred review work")
		core.callLater(0, self._runDeferredReviewWork)

	def _speakReviewMessage(self, text: str):
		"""Speak a review message and mark speech-sensitive work as blocked."""
		self.isReviewSpeechActive = True
		ui.message(text)

	def _deferMessagePrefetchUntilSpeechDone(
		self,
		state: dict[str, Any],
		direction: int,
		reason: str,
		chainCount: int = 0,
	):
		"""Start prefetch only after the boundary message has finished speaking."""
		if direction >= 0:
			return
		self.deferredPrefetchContext = {
			"chainCount": chainCount,
			"chatKey": self.activeChatKey,
			"direction": direction,
			"reason": reason,
			"state": state,
		}
		log.io(
			"WeChatEnhancement: "
			f"message prefetch deferred until speech done, reason={reason}, "
			f"currentIndex={state.get('currentIndex')}, "
			f"count={len(state.get('messages') or [])}, chainCount={chainCount}",
		)

	def _deferPendingReviewDrainUntilSpeechDone(self, reason: str):
		"""Read queued review gestures after the boundary message has finished speaking."""
		if not self.pendingReviewDirections:
			return
		self.isPendingReviewDrainDeferred = True
		log.io(
			"WeChatEnhancement: "
			f"pending review drain deferred until speech done, reason={reason}, "
			f"count={len(self.pendingReviewDirections)}",
		)

	def _runDeferredReviewWork(self):
		"""Run pending review or prefetch work once speech has completed."""
		if self._isSpeechActive():
			log.io("WeChatEnhancement: deferred review work waiting: speech still active")
			return
		if self.isBoundaryScrollPending:
			log.io("WeChatEnhancement: deferred review work skipped: boundary scroll pending")
			return
		if not self._isMessageInputFocus(logDetails=False):
			self._clearDeferredReviewWork("focus left message input")
			self._clearPendingReviewGestures("focus left message input")
			return
		if self.isPendingReviewDrainDeferred and self.pendingReviewDirections:
			self.isPendingReviewDrainDeferred = False
			log.io("WeChatEnhancement: running deferred pending review drain")
			self._schedulePendingReviewDrain()
			return

		context = self.deferredPrefetchContext
		self.deferredPrefetchContext = None
		if context is None:
			return
		if context.get("chatKey") != self.activeChatKey:
			log.io("WeChatEnhancement: deferred message prefetch discarded: chat changed")
			return
		if self.pendingReviewDirections:
			log.io("WeChatEnhancement: deferred message prefetch skipped: queued review gesture exists")
			return
		self._maybeStartMessagePrefetch(
			context["state"],
			context["direction"],
			context.get("chainCount", 0),
			reason=context["reason"],
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
			log.io(
				"WeChatEnhancement: "
				f"prefetch restored cursor by exact queue offset index={state['currentIndex']}",
			)
			return True
		return self._restoreReviewPosition(state, oldMessages, oldIndex)

	def _performMessagePrefetchScroll(self, context: dict[str, Any]) -> bool:
		"""Perform one background prefetch scroll."""
		messageList = context.get("messageList")
		if messageList is None:
			return False
		scrollSteps = winUser.WHEEL_DELTA * self.PREFETCH_WHEEL_UNITS
		log.io(
			"WeChatEnhancement: "
			f"message prefetch scroll, wheelUnits={self.PREFETCH_WHEEL_UNITS}, "
			f"scrollSteps={scrollSteps}",
		)
		return self._scrollMessageList(messageList, scrollSteps)

	def _shouldContinueMessagePrefetch(self, state: dict[str, Any], chainCount: int) -> bool:
		"""Return whether another background prefetch page should be requested."""
		if chainCount >= self.MAX_CHAINED_PREFETCHES:
			log.io(
				"WeChatEnhancement: "
				f"message prefetch chain stopped, chainCount={chainCount}",
			)
			return False
		currentIndex = state.get("currentIndex", -1)
		visibleStart = state.get("visibleStart")
		shouldContinue = (
			0 <= currentIndex < self.PREFETCH_TARGET_REMAINING
			and visibleStart is not None
			and visibleStart <= self.PREFETCH_VISIBLE_START_LIMIT
		)
		if shouldContinue:
			log.io(
				"WeChatEnhancement: "
				f"message prefetch chaining, currentIndex={currentIndex}, "
				f"target={self.PREFETCH_TARGET_REMAINING}, chainCount={chainCount}",
			)
		return shouldContinue

	def _scheduleChainedMessagePrefetch(self, state: dict[str, Any], chainCount: int):
		"""Start the next prefetch page after the current merge settles."""
		wx.CallLater(
			1,
			self._maybeStartMessagePrefetch,
			state,
			-1,
			chainCount,
			"chain",
		)

	def _finishMessagePrefetch(self, reason: str = "timer") -> dict[str, Any] | None:
		"""Merge one completed message prefetch when ordering is verified."""
		context = self.prefetchContext
		if not self.isPrefetchPending or context is None:
			return None
		if reason == "timer":
			self.prefetchLoadTimer = None
		if context.get("chatKey") != self.activeChatKey:
			self._cancelMessagePrefetch(f"{reason}: chat changed")
			return None

		oldMessages = context["oldMessages"]
		oldIndex = context["oldIndex"]
		oldVisibleSignature = context["oldVisibleSignature"]
		self.isPrefetchRefresh = True
		try:
			state = self.refreshMessageQueue(setNotificationBaseline=True, logDetails=False)
		finally:
			self.isPrefetchRefresh = False
		if not state or not state["messages"]:
			log.io(f"WeChatEnhancement: message prefetch discarded, reason={reason}, no refreshed messages")
			self._stopMessagePrefetchTimer()
			self._clearMessagePrefetchState()
			return state

		self._restorePrefetchReviewPosition(state, oldMessages, oldIndex)
		oldMessagesStart = self._findSubList(state["messages"], oldMessages)
		visibleChanged = (state.get("visibleSignature") or ()) != oldVisibleSignature
		if oldMessagesStart is None or oldMessagesStart <= 0:
			log.io(
				"WeChatEnhancement: "
				f"message prefetch discarded, reason={reason}, visibleChanged={visibleChanged}, "
				f"oldMessagesStart={oldMessagesStart}, count={len(state['messages'])}",
			)
			self._stopMessagePrefetchTimer()
			self._clearMessagePrefetchState()
			return state

		chainCount = context.get("chainCount", 0) + 1
		log.io(
			"WeChatEnhancement: "
			f"message prefetch merged, prepended={oldMessagesStart}, "
			f"count={len(state['messages'])}, currentIndex={state['currentIndex']}, "
			f"reason={reason}, chainCount={chainCount}",
		)
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
		reason: str = "review",
	):
		"""Start a quiet older-message prefetch after a verified boundary read."""
		if direction >= 0:
			return
		if self._isSpeechActive():
			log.io(
				"WeChatEnhancement: "
				f"message prefetch delayed: review speech active, reason={reason}, "
				f"chainCount={chainCount}",
			)
			self._deferMessagePrefetchUntilSpeechDone(state, direction, reason, chainCount)
			return
		if self.isBoundaryScrollPending or self.isPrefetchPending:
			log.io(
				"WeChatEnhancement: "
				f"message prefetch skipped, reason={reason}, "
				f"boundaryPending={self.isBoundaryScrollPending}, "
				f"prefetchPending={self.isPrefetchPending}",
			)
			return
		if self.activeChatKey is None or not state.get("messages"):
			log.io(
				"WeChatEnhancement: "
				f"message prefetch skipped, reason={reason}, no active messages",
			)
			return
		currentIndex = state["currentIndex"]
		if currentIndex < 0:
			log.io(
				"WeChatEnhancement: "
				f"message prefetch skipped, reason={reason}, currentIndex={currentIndex}",
			)
			return
		if currentIndex >= self.PREFETCH_TARGET_REMAINING:
			log.io(
				"WeChatEnhancement: "
				f"message prefetch skipped, reason={reason}, "
				f"currentIndex={currentIndex}, target={self.PREFETCH_TARGET_REMAINING}",
			)
			return
		visibleStart = state.get("visibleStart")
		if visibleStart is None or visibleStart > self.PREFETCH_VISIBLE_START_LIMIT:
			log.io(
				"WeChatEnhancement: "
				f"message prefetch skipped, reason={reason}, visibleStart={visibleStart}, "
				f"currentIndex={currentIndex}",
			)
			return

		messageList = self._findCurrentMessageList(logDetails=False)
		if messageList is None:
			log.io(f"WeChatEnhancement: message prefetch skipped, reason={reason}, no message list")
			return

		oldVisibleSignature = state.get("visibleSignature") or ()
		self.prefetchContext = {
			"chatKey": self.activeChatKey,
			"chainCount": chainCount,
			"messageList": messageList,
			"oldIndex": currentIndex,
			"oldMessages": list(state["messages"]),
			"oldVisibleSignature": oldVisibleSignature,
		}
		self.isPrefetchPending = True
		log.io(
			"WeChatEnhancement: "
			f"message prefetch start, reason={reason}, currentIndex={currentIndex}, "
			f"visibleStart={visibleStart}, count={len(state['messages'])}, "
			f"wheelUnits={self.PREFETCH_WHEEL_UNITS}, "
			f"chainCount={chainCount}",
		)
		if not self._performMessagePrefetchScroll(self.prefetchContext):
			self._cancelMessagePrefetch("scroll failed")
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
			log.io(
				"WeChatEnhancement: "
				f"restoreReviewPosition matched index={bestIndex}, score={bestScore}",
			)
			return True
		log.io(
			"WeChatEnhancement: "
			f"restoreReviewPosition no match for anchor={self._shortenForLog(anchorText)!r}",
		)
		return False

	def _speakRelativeMessage(self, state: dict[str, Any], direction: int) -> bool:
		"""Speak a message relative to the current review cursor."""
		nextIndex = state["currentIndex"] + direction
		if nextIndex < 0 or nextIndex >= len(state["messages"]):
			log.io(
				"WeChatEnhancement: "
				f"speakRelativeMessage out of range, currentIndex={state['currentIndex']}, "
				f"direction={direction}, count={len(state['messages'])}",
			)
			return False
		state["currentIndex"] = nextIndex
		log.io(
			"WeChatEnhancement: "
			f"speakRelativeMessage index={nextIndex}, "
			f"text={self._shortenForLog(state['messages'][nextIndex])!r}",
		)
		self._speakReviewMessage(state["messages"][nextIndex])
		return True

	def _speakMessageAtIndex(self, state: dict[str, Any], index: int, reason: str) -> bool:
		"""Speak a message at an exact queue index."""
		if index < 0 or index >= len(state["messages"]):
			log.io(
				"WeChatEnhancement: "
				f"speakMessageAtIndex out of range, index={index}, "
				f"count={len(state['messages'])}, reason={reason}",
			)
			return False
		state["currentIndex"] = index
		log.io(
			"WeChatEnhancement: "
			f"speakMessageAtIndex index={index}, reason={reason}, "
			f"text={self._shortenForLog(state['messages'][index])!r}",
		)
		self._speakReviewMessage(state["messages"][index])
		return True

	def _speakCurrentMessage(self, state: dict[str, Any]) -> bool:
		"""Speak the current review message without moving the review cursor."""
		currentIndex = state["currentIndex"]
		if currentIndex < 0 or currentIndex >= len(state["messages"]):
			log.io(
				"WeChatEnhancement: "
				f"speakCurrentMessage out of range, currentIndex={currentIndex}, "
				f"count={len(state['messages'])}",
			)
			return False
		log.io(
			"WeChatEnhancement: "
			f"speakCurrentMessage index={currentIndex}, "
			f"text={self._shortenForLog(state['messages'][currentIndex])!r}",
		)
		self._speakReviewMessage(state["messages"][currentIndex])
		return True

	def _handleBoundaryLoadFailure(self, state: dict[str, Any] | None, direction: int, reason: str):
		"""Keep the review cursor stable when older or newer messages do not load."""
		if state is None:
			log.io(
				"WeChatEnhancement: "
				f"boundary load failed without active state, direction={direction}, reason={reason}",
			)
			return
		log.io(
			"WeChatEnhancement: "
			f"boundary load failed silently, direction={direction}, reason={reason}, "
			f"currentIndex={state.get('currentIndex')}, count={len(state.get('messages') or [])}",
		)

	def _shouldScrollBeforeRelativeRead(self, state: dict[str, Any], direction: int) -> bool:
		"""Return whether the next review step should first move the visible list."""
		nextIndex = state["currentIndex"] + direction
		shouldScroll = nextIndex < 0 or nextIndex >= len(state["messages"])
		if shouldScroll:
			log.io(
				"WeChatEnhancement: "
				f"shouldScrollBeforeRelativeRead=True, currentIndex={state['currentIndex']}, "
				f"direction={direction}, nextIndex={nextIndex}, count={len(state['messages'])}",
			)
		return shouldScroll

	def _isBoundaryTargetVisible(
		self,
		state: dict[str, Any],
		targetIndex: int | None,
	) -> bool:
		"""Return whether a boundary scroll exposed the next review target."""
		visibleStart = state.get("visibleStart")
		visibleEnd = state.get("visibleEnd")
		if visibleStart is None or visibleEnd is None:
			log.io("WeChatEnhancement: boundary target check skipped: no visible range")
			return False

		if targetIndex is not None:
			isVisible = visibleStart <= targetIndex <= visibleEnd
			log.io(
				"WeChatEnhancement: "
				f"boundary target index={targetIndex}, visibleRange=({visibleStart}, {visibleEnd}), "
				f"isVisible={isVisible}",
			)
			return isVisible

		log.io("WeChatEnhancement: boundary target unavailable")
		return False

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
		targetIndex = None
		if oldMessagesStart is not None and 0 <= targetOldIndex < len(oldMessages):
			targetIndex = oldMessagesStart + targetOldIndex
		elif oldMessagesStart is not None and direction < 0 and targetOldIndex < 0:
			targetIndex = oldMessagesStart - 1
		elif oldMessagesStart is not None and direction > 0 and targetOldIndex >= len(oldMessages):
			targetIndex = oldMessagesStart + len(oldMessages)

		log.io(
			"WeChatEnhancement: "
			f"boundary target decision oldMessagesStart={oldMessagesStart}, "
			f"targetOldIndex={targetOldIndex}, targetIndex={targetIndex}",
		)
		return targetIndex

	def _hasVisibleWindowChanged(
		self,
		state: dict[str, Any],
		oldVisibleSignature: tuple[tuple[str, tuple[int, int, int, int] | None], ...],
	) -> bool:
		"""Return whether the message list shows a different visible window."""
		newVisibleSignature = state.get("visibleSignature") or ()
		visibleChanged = bool(newVisibleSignature) and newVisibleSignature != oldVisibleSignature
		log.io(
			"WeChatEnhancement: "
			f"visible window changed={visibleChanged}, "
			f"oldVisibleCount={len(oldVisibleSignature)}, "
			f"newVisibleCount={len(newVisibleSignature)}",
		)
		return visibleChanged

	def _readAfterBoundaryScroll(
		self,
		direction: int,
		oldMessages: list[str],
		oldIndex: int,
		oldVisibleMessages: list[str],
		oldVisibleSignature: tuple[tuple[str, tuple[int, int, int, int] | None], ...],
		nextAttempt: int,
	):
		"""Refresh after a boundary scroll and read the requested relative message."""
		scheduledRetry = False
		prefetchState = None
		shouldLogAttemptDetails = (
			nextAttempt <= 1
			or nextAttempt % 4 == 0
			or nextAttempt > self.MAX_BOUNDARY_SCROLL_ATTEMPTS
		)
		log.io(
			"WeChatEnhancement: "
			f"readAfterBoundaryScroll start, direction={direction}, "
			f"oldIndex={oldIndex}, oldCount={len(oldMessages)}, "
			f"oldVisibleCount={len(oldVisibleMessages)}, nextAttempt={nextAttempt}",
		)
		if shouldLogAttemptDetails:
			self._logQueueSnapshot("before boundary scroll queue", oldMessages)
			self._logQueueSnapshot("before boundary scroll visible", oldVisibleMessages)
		try:
			if not self._isMessageInputFocus(logDetails=False):
				log.io("WeChatEnhancement: readAfterBoundaryScroll stopped: focus left message input")
				self._clearPendingReviewGestures("focus left message input during boundary scroll")
				return
			state = self.refreshMessageQueue(
				setNotificationBaseline=True,
				logDetails=shouldLogAttemptDetails,
			)
			if not state or not state["messages"]:
				log.io("WeChatEnhancement: readAfterBoundaryScroll no refreshed messages")
				self._handleBoundaryLoadFailure(state, direction, "no refreshed messages")
				return

			targetIndex = self._getBoundaryTargetIndex(state, oldMessages, oldIndex, direction)
			targetWasCached = 0 <= oldIndex + direction < len(oldMessages)
			targetVisible = self._isBoundaryTargetVisible(state, targetIndex)
			log.io(
				"WeChatEnhancement: "
				f"readAfterBoundaryScroll refreshed count={len(state['messages'])}, "
				f"index={state['currentIndex']}, targetWasCached={targetWasCached}, "
				f"targetVisible={targetVisible}",
			)
			visibleChanged = self._hasVisibleWindowChanged(state, oldVisibleSignature)
			if visibleChanged and not shouldLogAttemptDetails:
				self._logQueueSnapshot(
					"after boundary scroll visible",
					state.get("visibleMessages") or [],
				)
			if targetIndex is not None and (targetWasCached or targetVisible):
				log.io("WeChatEnhancement: readAfterBoundaryScroll speaking exact boundary target")
				if self._speakMessageAtIndex(state, targetIndex, "boundary target"):
					prefetchState = state
					return
			if targetWasCached:
				log.io("WeChatEnhancement: readAfterBoundaryScroll speaking cached target")
				restored = self._restoreReviewPosition(state, oldMessages, oldIndex)
				if not restored:
					oldMessagesStart = self._findSubList(state["messages"], oldMessages)
					if oldMessagesStart is not None:
						state["currentIndex"] = oldMessagesStart + oldIndex
					else:
						state["currentIndex"] = min(oldIndex, len(state["messages"]) - 1)
					log.io(
						"WeChatEnhancement: "
						f"readAfterBoundaryScroll restored cached fallback index={state['currentIndex']}",
					)
				if self._speakRelativeMessage(state, direction):
					prefetchState = state
					return
				log.io("WeChatEnhancement: readAfterBoundaryScroll cached target could not be read")
				return
			if not targetVisible and nextAttempt <= self.MAX_BOUNDARY_SCROLL_ATTEMPTS:
				log.io(
					"WeChatEnhancement: "
					f"readAfterBoundaryScroll target not visible; retrying attempt={nextAttempt}",
				)
				scheduledRetry = True
				self._scrollBoundaryAndRead(state, direction, attempt=nextAttempt)
				return

			if not targetVisible and not targetWasCached:
				restored = self._restoreReviewPosition(state, oldMessages, oldIndex)
				if not restored:
					oldMessagesStart = self._findSubList(state["messages"], oldMessages)
					if oldMessagesStart is not None:
						state["currentIndex"] = oldMessagesStart + oldIndex
				log.io(
					"WeChatEnhancement: "
					"readAfterBoundaryScroll stopped without a confirmed list boundary",
				)
				self._handleBoundaryLoadFailure(state, direction, "target not visible")
				return

			log.io("WeChatEnhancement: readAfterBoundaryScroll finished without a confirmed target")
			self._handleBoundaryLoadFailure(state, direction, "no confirmed target")
		finally:
			if not scheduledRetry:
				self.isBoundaryScrollPending = False
				if prefetchState is not None:
					if self.pendingReviewDirections:
						self._deferPendingReviewDrainUntilSpeechDone("boundaryRead")
					else:
						self._deferMessagePrefetchUntilSpeechDone(prefetchState, direction, "boundaryRead")
				elif self.pendingReviewDirections:
					log.io(
						"WeChatEnhancement: "
						"boundary load did not produce a message; retrying queued review gesture",
					)
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
			log.io(
				"WeChatEnhancement: "
				f"scroll attempt {attempt}: mouse wheel units={wheelUnits}, "
				f"scrollSteps={scrollSteps}",
			)
			return self._scrollMessageList(messageList, scrollSteps)
		log.io(f"WeChatEnhancement: no scroll strategy for attempt={attempt}")
		return False

	def _scrollBoundaryAndRead(self, state: dict[str, Any], direction: int, attempt: int = 0):
		"""Scroll beyond the visible boundary and read after WeChat updates the list."""
		log.io(
			"WeChatEnhancement: "
			f"scrollBoundaryAndRead start, direction={direction}, "
			f"currentIndex={state['currentIndex']}, count={len(state['messages'])}, "
			f"attempt={attempt}",
		)
		if self.isPrefetchPending:
			self._cancelMessagePrefetch("boundary scroll")
		messageList = self._findCurrentMessageList()
		if messageList is None:
			log.io("WeChatEnhancement: scrollBoundaryAndRead no message list")
			self.isBoundaryScrollPending = False
			self._clearPendingReviewGestures("boundary scroll has no message list")
			self._handleBoundaryLoadFailure(state, direction, "no message list")
			return

		oldVisibleMessages = list(state.get("visibleMessages") or [])
		oldVisibleSignature = state.get("visibleSignature") or ()
		if not oldVisibleMessages:
			records = self._collectVisibleMessageRecords(messageList)
			oldVisibleMessages = [
				record["text"] for record in records
			]
			oldVisibleSignature = self._getVisibleWindowSignature(records)
		self._logQueueSnapshot("scrollBoundaryAndRead old visible", oldVisibleMessages)

		oldMessages = list(state["messages"])
		oldIndex = state["currentIndex"]
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			log.io("WeChatEnhancement: scrollBoundaryAndRead stopping previous timer")
			self.scrollLoadTimer.Stop()
		self.isBoundaryScrollPending = True

		if not self._performScrollAttempt(messageList, direction, attempt):
			log.io(
				"WeChatEnhancement: "
				f"scrollBoundaryAndRead attempt={attempt} failed",
			)
			if attempt < self.MAX_BOUNDARY_SCROLL_ATTEMPTS:
				self._scrollBoundaryAndRead(state, direction, attempt=attempt + 1)
				return
			self.isBoundaryScrollPending = False
			self._clearPendingReviewGestures("boundary scroll attempt failed")
			self._handleBoundaryLoadFailure(state, direction, "scroll attempt failed")
			return

		self.scrollLoadTimer = wx.CallLater(
			self.SCROLL_LOAD_DELAY,
			self._readAfterBoundaryScroll,
			direction,
			oldMessages,
			oldIndex,
			oldVisibleMessages,
			oldVisibleSignature,
			attempt + 1,
		)
		log.io(
			"WeChatEnhancement: "
			f"scrollBoundaryAndRead timer scheduled delay={self.SCROLL_LOAD_DELAY}ms",
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
		state = self.refreshMessageQueue(
			messageList,
			logDetails=False,
		)
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
			log.debug(f"New message validated. Name: '{latestRecord['text']}'")
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
			self._cancelMessagePrefetch("message focus")
			log.io("WeChatEnhancement: event_gainFocus on message item")
			self.notifyUserActivity()
			self.refreshMessageQueue(obj.parent, setNotificationBaseline=True)
		elif self._isMessageList(obj):
			self._cancelMessagePrefetch("message list focus")
			log.io("WeChatEnhancement: event_gainFocus on message list")
			self.refreshMessageQueue(obj, setNotificationBaseline=True)
		nextHandler()

	def notifyUserActivity(self):
		"""Mark the user as active and pause automatic reading briefly."""
		self.isUserActive = True
		if self.activityTimer and self.activityTimer.IsRunning():
			self.activityTimer.Restart(2000)
		else:
			self.activityTimer = wx.CallLater(2000, self._clearUserActivityFlag)
		log.debug("User activity detected; auto-reading paused for 2 seconds.")

	def _clearUserActivityFlag(self):
		"""Clear the temporary user activity flag."""
		self.isUserActive = False
		log.debug("User activity timeout. Resuming auto-reading.")

	def performNotification(self, messageObj: Any):
		"""Play the configured notification for a new message."""
		playWaveFile(self.SOUND_NEW_MESSAGE)
		if self.notificationMode == self.MODE_SOUND_AND_SPEECH:
			ui.message(messageObj.name)

	def _getObjectSummaryForLog(self, obj: Any | None) -> str:
		"""Return a concise object summary for diagnostic logs."""
		if obj is None:
			return "<None>"
		parts = []
		for attr in ("role", "UIAAutomationId", "name", "windowClassName"):
			try:
				value = getattr(obj, attr)
			except Exception:
				continue
			if value is None or value == "":
				continue
			parts.append(f"{attr}={self._shortenForLog(value)!r}")
		return ", ".join(parts) if parts else repr(obj)

	def _isEditableObject(self, obj: Any) -> bool:
		"""Return whether an object behaves like an editable text field."""
		try:
			states = obj.states
		except Exception:
			states = set()
		try:
			if controlTypes.State.EDITABLE in states:
				return True
		except Exception:
			pass
		try:
			role = obj.role
		except Exception:
			return False
		return role in (controlTypes.Role.EDITABLETEXT, controlTypes.Role.RICHEDIT)

	def _isInMessageInputRegion(self, obj: Any, messageList: Any) -> bool:
		"""Return whether an editable focus is located in the chat input region."""
		objRect = self._getObjectLocation(obj)
		listRect = self._getObjectLocation(messageList)
		if objRect is None or listRect is None:
			log.io(
				"WeChatEnhancement: "
				"message input geometry unavailable; accepting editable focus",
			)
			return True

		objLeft, objTop, objWidth, _objHeight = objRect
		listLeft, listTop, listWidth, listHeight = listRect
		objCenterX = objLeft + int(objWidth / 2)
		listRight = listLeft + listWidth
		listBottom = listTop + listHeight
		isInChatColumn = listLeft - 40 <= objCenterX <= listRight + 40
		isBelowMessageList = objTop >= listBottom - 40
		if isInChatColumn and isBelowMessageList:
			return True

		log.io(
			"WeChatEnhancement: "
			f"editable focus rejected by geometry, focusRect={objRect!r}, "
			f"messageListRect={listRect!r}",
		)
		return False

	def _isMessageInputFocus(self, logDetails: bool = True) -> bool:
		"""Return whether the current focus is the WeChat chat message input."""
		try:
			focus = api.getFocusObject()
		except Exception as e:
			if logDetails:
				log.io(f"WeChatEnhancement: review gesture passed through: no focus object, error={e!r}")
			return False

		if focus is None:
			if logDetails:
				log.io("WeChatEnhancement: review gesture passed through: focus is None")
			return False

		if self._getContainingMessageList(focus) is not None:
			if logDetails:
				log.io(
					"WeChatEnhancement: "
					"review gesture passed through: focus is inside message list, "
					f"focus={self._getObjectSummaryForLog(focus)}",
				)
			return False

		if not self._isEditableObject(focus):
			if logDetails:
				log.io(
					"WeChatEnhancement: "
					"review gesture passed through: focus is not editable, "
					f"focus={self._getObjectSummaryForLog(focus)}",
				)
			return False

		messageList = self._findCurrentMessageList(logDetails=False)
		if messageList is None:
			if logDetails:
				log.io(
					"WeChatEnhancement: "
					"review gesture passed through: editable focus has no message list, "
					f"focus={self._getObjectSummaryForLog(focus)}",
				)
			return False

		if not self._isInMessageInputRegion(focus, messageList):
			if logDetails:
				log.io(
					"WeChatEnhancement: "
					"review gesture passed through: editable focus is outside message input, "
					f"focus={self._getObjectSummaryForLog(focus)}",
				)
			return False

		return True

	def _sendReviewGestureThrough(self, gesture: Any, reason: str):
		"""Let WeChat or NVDA handle a review gesture outside the message input."""
		log.io(f"WeChatEnhancement: review gesture sent through, reason={reason}")
		self._clearPendingReviewGestures(reason)
		self._clearDeferredReviewWork(reason)
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
			self.scrollLoadTimer = None
		if self.isBoundaryScrollPending:
			log.io(f"WeChatEnhancement: boundary scroll cancelled, reason={reason}")
			self.isBoundaryScrollPending = False
		try:
			gesture.send()
		except Exception:
			log.debugWarning("Unable to send WeChat review gesture through.", exc_info=True)

	def _clearPendingReviewGestures(self, reason: str):
		"""Clear queued review gestures and stop their drain timer."""
		if self.pendingReviewDirections:
			log.io(
				"WeChatEnhancement: "
				f"pending review gestures cleared, reason={reason}, "
				f"count={len(self.pendingReviewDirections)}",
			)
		self.pendingReviewDirections = []
		if self.pendingReviewDrainTimer and self.pendingReviewDrainTimer.IsRunning():
			self.pendingReviewDrainTimer.Stop()
		self.pendingReviewDrainTimer = None

	def _queuePendingReviewGesture(self, direction: int):
		"""Queue a review movement while a boundary scroll is loading."""
		if len(self.pendingReviewDirections) >= self.MAX_PENDING_REVIEW_GESTURES:
			self.pendingReviewDirections[-1] = direction
			log.io(
				"WeChatEnhancement: "
				f"pending review gesture coalesced, direction={direction}, "
				f"count={len(self.pendingReviewDirections)}",
			)
			return
		self.pendingReviewDirections.append(direction)
		log.io(
			"WeChatEnhancement: "
			f"pending review gesture queued, direction={direction}, "
			f"count={len(self.pendingReviewDirections)}",
		)

	def _schedulePendingReviewDrain(self):
		"""Schedule queued review gestures to run after a boundary load speaks."""
		if not self.pendingReviewDirections:
			return
		if self.pendingReviewDrainTimer and self.pendingReviewDrainTimer.IsRunning():
			return
		self.pendingReviewDrainTimer = wx.CallLater(
			self.PENDING_REVIEW_DRAIN_DELAY,
			self._drainPendingReviewGestures,
		)
		log.io(
			"WeChatEnhancement: "
			f"pending review drain scheduled, delay={self.PENDING_REVIEW_DRAIN_DELAY}ms, "
			f"count={len(self.pendingReviewDirections)}",
		)

	def _readReviewDirection(self, state: dict[str, Any], direction: int) -> bool:
		"""Read one relative review movement, scrolling only at a queue boundary."""
		if direction > 0 and state["currentIndex"] >= len(state["messages"]) - 1:
			log.io("WeChatEnhancement: readReviewDirection at newest cached message")
			return self._speakCurrentMessage(state)
		if self._shouldScrollBeforeRelativeRead(state, direction):
			self._scrollBoundaryAndRead(state, direction)
			return False
		return self._speakRelativeMessage(state, direction)

	def _drainPendingReviewGestures(self):
		"""Read queued review gestures in order after the boundary message is available."""
		self.pendingReviewDrainTimer = None
		if not self.pendingReviewDirections:
			return
		if self.isBoundaryScrollPending:
			log.io("WeChatEnhancement: pending review drain deferred: boundary scroll pending")
			return
		if not self._isMessageInputFocus(logDetails=True):
			self._clearPendingReviewGestures("focus left message input")
			return

		prefetchedState = None
		if self.isPrefetchPending:
			prefetchedState = self._finishMessagePrefetch("pendingReview")
		state = prefetchedState or self._getActiveChatState(refresh=False)
		if state is None:
			self._clearPendingReviewGestures("no active chat state")
			return

		direction = self.pendingReviewDirections.pop(0)
		log.io(
			"WeChatEnhancement: "
			f"pending review gesture draining, direction={direction}, "
			f"remaining={len(self.pendingReviewDirections)}",
		)
		didSpeak = self._readReviewDirection(state, direction)
		if self.isBoundaryScrollPending:
			return
		if not didSpeak:
			self._clearPendingReviewGestures("queued read did not speak")
			return
		if self.pendingReviewDirections:
			self._deferPendingReviewDrainUntilSpeechDone("pendingReview")
		elif direction < 0:
			self._deferMessagePrefetchUntilSpeechDone(state, direction, "pendingReview")

	def _getActiveChatState(self, refresh: bool = True) -> dict[str, Any] | None:
		"""Return the refreshed state for the active chat."""
		if refresh:
			state = self.refreshMessageQueue(setNotificationBaseline=True, logDetails=False)
		else:
			state = self.chatStates.get(self.activeChatKey)
		if state and state["messages"]:
			log.io(
				"WeChatEnhancement: "
				f"active chat state count={len(state['messages'])}, "
				f"currentIndex={state['currentIndex']}",
			)
			return state
		log.io("WeChatEnhancement: active chat state unavailable")
		ui.message(self.NO_MESSAGES_TEXT)
		return None

	@script(
		# Translators: Description for the command that reads the previous WeChat message.
		description=_("Reads the previous message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+upArrow",
	)
	def script_readPreviousMessage(self, gesture: Any):
		"""Read the previous message from the active chat queue."""
		log.io("WeChatEnhancement: script_readPreviousMessage")
		if not self._isMessageInputFocus():
			self._sendReviewGestureThrough(gesture, "focus is not message input")
			return
		if self.isBoundaryScrollPending:
			self._queuePendingReviewGesture(-1)
			return
		self._clearPendingReviewGestures("manual read previous")
		self._clearDeferredReviewWork("manual read previous")
		prefetchedState = None
		if self.isPrefetchPending:
			prefetchedState = self._finishMessagePrefetch("readPrevious")
		state = prefetchedState or self._getActiveChatState(refresh=not self.isPrefetchPending)
		if state is None:
			return
		self._readReviewDirection(state, -1)

	@script(
		# Translators: Description for the command that reads the next WeChat message.
		description=_("Reads the next message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+downArrow",
	)
	def script_readNextMessage(self, gesture: Any):
		"""Read the next message from the active chat queue."""
		log.io("WeChatEnhancement: script_readNextMessage")
		if not self._isMessageInputFocus():
			self._sendReviewGestureThrough(gesture, "focus is not message input")
			return
		if self.isBoundaryScrollPending:
			self._queuePendingReviewGesture(1)
			return
		self._clearPendingReviewGestures("manual read next")
		self._clearDeferredReviewWork("manual read next")
		prefetchedState = None
		if self.isPrefetchPending:
			prefetchedState = self._finishMessagePrefetch("readNext")
		state = prefetchedState or self._getActiveChatState(refresh=not self.isPrefetchPending)
		if state is None:
			return
		self._readReviewDirection(state, 1)

	@script(
		# Translators: Description for the command that reads the last WeChat message.
		description=_("Reads the last message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+end",
	)
	def script_readLastMessage(self, gesture: Any):
		"""Read the newest message from the active chat queue."""
		log.io("WeChatEnhancement: script_readLastMessage")
		if not self._isMessageInputFocus():
			self._sendReviewGestureThrough(gesture, "focus is not message input")
			return
		self._clearPendingReviewGestures("read last")
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
			self.scrollLoadTimer = None
		if self.isBoundaryScrollPending:
			log.io("WeChatEnhancement: boundary scroll cancelled, reason=read last")
			self.isBoundaryScrollPending = False
		self._clearDeferredReviewWork("read last")
		if self.isPrefetchPending:
			self._cancelMessagePrefetch("read last")
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
