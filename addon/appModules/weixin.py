# -*- coding: utf-8 -*-
# PC WeChat add-on for NVDA
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.
# Copyright (C) 2025 Cary-rowen <manchen_0528@outlook.com>

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from os.path import dirname, join
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, TypeAlias

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
from comtypes import COMError
from comInterfaces import UIAutomationClient as UIA

from logHandler import log
from NVDAObjects import NVDAObject
from NVDAObjects.UIA import UIA as UIAObject
from nvwave import playWaveFile
from scriptHandler import script
from UIAHandler.utils import createUIAMultiPropertyCondition

from ._weixinMessageInput import WeChatMessageInput

if TYPE_CHECKING:
	import inputCore
	from typing import override

else:

	def override(method: Any) -> Any:
		"""Return overridden methods unchanged at runtime."""
		return method


addonHandler.initTranslation()


ChatIdentity: TypeAlias = tuple[Literal["single", "main"], str]
MessageIdentity: TypeAlias = tuple[Literal["uiaRuntime"], tuple[int, ...]]
NextHandler: TypeAlias = Callable[[], None]
PressedAltKey: TypeAlias = tuple[int, int]
UIABounds: TypeAlias = tuple[int, int, int, int]
PositionedUIAObject: TypeAlias = tuple[UIAObject, UIABounds]


class MessageRecord(NamedTuple):
	"""Accessible text and identity for a visible WeChat message."""

	identity: MessageIdentity
	text: str


@dataclass
class ReviewState:
	"""Temporary message review queue state."""

	messages: list[MessageRecord]
	currentIndex: int = -1


class AppModule(appModuleHandler.AppModule):
	"""App module for PC WeChat enhancements."""

	MESSAGE_LIST_UIA_ID = "chat_message_list"
	MESSAGE_INPUT_UIA_ID = "chat_input_field"
	MESSAGE_TOOLBAR_UIA_ID = "tool_bar_accessible"
	MESSAGE_ITEM_UIA_ID = "chat_message_list.qt_scrollarea_viewport.chat_bubble_item_view"
	MESSAGE_TIME_ITEM_UIA_CLASS = "mmui::ChatItemView"
	SESSION_LIST_UIA_ID = "session_list"
	SESSION_LIST_UIA_CLASS = "mmui::XTableView"
	CONTACT_LIST_UIA_ID = "primary_table_.contact_list"
	CONTACT_LIST_UIA_CLASS = "mmui::StickyHeaderRecyclerListView"
	SEARCH_EDIT_UIA_CLASS = "mmui::XValidatorTextEdit"
	VOIP_TRAY_WINDOW_UIA_ID = "VOIPTrayWindow"
	VOIP_TRAY_WINDOW_UIA_CLASS = "mmui::VOIPTrayWindow"
	SEARCH_RESULT_WINDOW_CLASS_NAME = "Qt51514QWindowToolSaveBits"
	MAIN_WINDOW_UIA_CLASS = "mmui::MainWindow"
	SINGLE_CHAT_WINDOW_UIA_CLASS = "mmui::ChatSingleWindow"
	CONFIG_SECTION = "weixin"
	CONFIG_KEY_NOTIFICATION_MODE = "notificationMode"
	MAX_MESSAGE_QUEUE_SIZE = 500
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

	def __init__(self, *args: Any, **kwargs: Any) -> None:
		super().__init__(*args, **kwargs)
		self.notificationMode: int = config.conf[self.CONFIG_SECTION][self.CONFIG_KEY_NOTIFICATION_MODE]
		self.isNotificationSuppressed: bool = False
		self.notificationSuppressionTimer = None
		self.lastNotifiedMessageRecord: MessageRecord | None = None
		self.reviewState = ReviewState(messages=[])
		self.reviewQueueUpdateOnLastRefresh: str = self.QUEUE_UPDATE_UNCHANGED
		self.activeChatIdentity: ChatIdentity | None = None
		self.activeMessageList: UIAObject | None = None
		self.scrollLoadTimer = None
		self.isBoundaryScrollPending: bool = False

		eventHandler.requestEvents(
			"gainFocus",
			processId=self.processID,
			windowClassName=self.SEARCH_RESULT_WINDOW_CLASS_NAME,
		)

	@override
	def terminate(self) -> None:
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
		self.scrollLoadTimer = None
		if self.notificationSuppressionTimer and self.notificationSuppressionTimer.IsRunning():
			self.notificationSuppressionTimer.Stop()
		self.notificationSuppressionTimer = None
		super().terminate()

	@override
	def chooseNVDAObjectOverlayClasses(self, obj: NVDAObject, clsList: list[type[NVDAObject]]) -> None:
		"""Add WeChat-specific object overlays."""
		if not isinstance(obj, UIAObject):
			return
		if (
			getattr(obj, "UIAAutomationId", None) != self.MESSAGE_INPUT_UIA_ID
			or getattr(obj, "UIAFrameworkId", None) != "Qt"
			or obj.role != controlTypes.Role.EDITABLETEXT
		):
			return
		try:
			textPattern: Any = getattr(obj, "UIATextPattern", None)
		except COMError:
			return
		if textPattern:
			clsList.insert(0, WeChatMessageInput)

	def _findMessageListAfterInput(self, inputObj: UIAObject) -> UIAObject | None:
		try:
			toolbar = inputObj.simpleNext
			messageList = toolbar.simpleNext
		except Exception:
			return None
		if (
			isinstance(toolbar, UIAObject)
			and toolbar.role == controlTypes.Role.TOOLBAR
			and toolbar.UIAAutomationId == self.MESSAGE_TOOLBAR_UIA_ID
			and isinstance(messageList, UIAObject)
			and messageList.role == controlTypes.Role.LIST
			and messageList.UIAAutomationId == self.MESSAGE_LIST_UIA_ID
		):
			self.activeMessageList = messageList
			return messageList
		return None

	def _getCurrentMessageList(self, focus: UIAObject | None = None) -> UIAObject | None:
		if focus is None:
			focus = api.getFocusObject()
		if not isinstance(focus, UIAObject) or focus.UIAAutomationId != self.MESSAGE_INPUT_UIA_ID:
			return None
		messageList = self.activeMessageList
		if messageList is not None:
			try:
				if (
					isinstance(messageList, UIAObject)
					and messageList.role == controlTypes.Role.LIST
					and messageList.UIAAutomationId == self.MESSAGE_LIST_UIA_ID
				):
					return messageList
			except Exception:
				self.activeMessageList = None
		return self._findMessageListAfterInput(focus)

	def _getMainWindowObject(self) -> UIAObject | None:
		"""Return the foreground WeChat main window."""
		foreground = api.getForegroundObject()
		if not isinstance(foreground, UIAObject):
			return None
		try:
			if foreground.UIAElement.CachedClassName != self.MAIN_WINDOW_UIA_CLASS:
				return None
		except Exception:
			return None
		return foreground

	def _getMatchingUIAObject(
		self,
		role: controlTypes.Role,
		obj: NVDAObject | None,
		className: str,
		automationId: str | None = None,
	) -> PositionedUIAObject | None:
		"""Return matching visible focusable UIA object details."""
		if not isinstance(obj, UIAObject):
			return None
		try:
			if (
				obj.role != role
				or not obj.isFocusable
				or obj.UIAElement.CachedClassName != className
				or (automationId is not None and obj.UIAAutomationId != automationId)
			):
				return None
			left, top, width, height = obj.location
			if width <= 0 or height <= 0:
				return None
		except Exception:
			return None
		return obj, (left, top, left + width, top + height)

	def _getVisibleFocusableUIAObject(
		self,
		elements: UIA.IUIAutomationElementArray,
		index: int,
		role: controlTypes.Role,
		className: str,
		automationId: str | None = None,
	) -> PositionedUIAObject | None:
		"""Return a matching UIA object and its bounds from an element array."""
		try:
			element = elements.getElement(index).buildUpdatedCache(UIAHandler.handler.baseCacheRequest)
			obj = UIAObject(UIAElement=element)
		except Exception:
			log.debugWarning("Unable to create a WeChat main window child object.", exc_info=True)
			return None
		return self._getMatchingUIAObject(role, obj, className, automationId)

	def _getMainWindowDescendants(
		self,
		controlType: int,
		role: controlTypes.Role,
		className: str,
		automationId: str | None = None,
	) -> list[PositionedUIAObject]:
		"""Return matching main-window descendants, collected from the end."""
		mainWindow = self._getMainWindowObject()
		if mainWindow is None:
			return []
		properties = {
			UIA.UIA_ControlTypePropertyId: controlType,
			UIA.UIA_ClassNamePropertyId: className,
			UIA.UIA_IsKeyboardFocusablePropertyId: True,
		}
		if automationId is not None:
			properties[UIA.UIA_AutomationIdPropertyId] = automationId
		try:
			condition = createUIAMultiPropertyCondition(properties)
			elements = mainWindow.UIAElement.findAll(UIAHandler.TreeScope_Descendants, condition)
			elementCount = elements.length
		except Exception:
			log.debugWarning("Unable to find a WeChat main window descendant.", exc_info=True)
			return []
		candidates = []
		for index in range(elementCount - 1, -1, -1):
			candidate = self._getVisibleFocusableUIAObject(
				elements,
				index,
				role,
				className,
				automationId,
			)
			if candidate is not None:
				candidates.append(candidate)
		return candidates

	def _findMainWindowDescendant(
		self,
		controlType: int,
		role: controlTypes.Role,
		className: str,
		automationId: str | None = None,
	) -> UIAObject | None:
		"""Find a visible focusable main-window descendant, searching from the end."""
		candidates = self._getMainWindowDescendants(controlType, role, className, automationId)
		if not candidates:
			return None
		return candidates[0][0]

	def _doBoundsVerticallyOverlap(self, firstBounds: UIABounds, secondBounds: UIABounds) -> bool:
		"""Return whether two bounds overlap on the vertical axis."""
		_firstLeft, firstTop, _firstRight, firstBottom = firstBounds
		_secondLeft, secondTop, _secondRight, secondBottom = secondBounds
		return firstTop < secondBottom and secondTop < firstBottom

	def _findForegroundSibling(
		self,
		role: controlTypes.Role,
		className: str,
		automationId: str,
	) -> UIAObject | None:
		"""Find a matching object adjacent to the foreground object."""
		foreground = api.getForegroundObject()
		candidate = self._getMatchingUIAObject(role, foreground, className, automationId)
		if candidate is not None:
			return candidate[0]
		for relation in ("simplePrevious", "simpleNext"):
			try:
				obj = getattr(foreground, relation)
			except Exception:
				continue
			candidate = self._getMatchingUIAObject(role, obj, className, automationId)
			if candidate is not None:
				return candidate[0]
		return None

	def _findOfficialAccountList(self) -> UIAObject | None:
		"""Find the Official Accounts list by its position in the main window."""
		candidates = self._getMainWindowDescendants(
			UIA.UIA_ListControlTypeId,
			controlTypes.Role.LIST,
			self.SESSION_LIST_UIA_CLASS,
			self.SESSION_LIST_UIA_ID,
		)
		for obj, bounds in candidates:
			left, _top, _right, _bottom = bounds
			for _referenceObj, referenceBounds in candidates:
				_referenceLeft, _referenceTop, referenceRight, _referenceBottom = referenceBounds
				if referenceRight <= left and self._doBoundsVerticallyOverlap(referenceBounds, bounds):
					return obj
		return None

	def _findContactList(self) -> UIAObject | None:
		"""Find the Contacts list in the main window."""
		return self._findMainWindowDescendant(
			UIA.UIA_ListControlTypeId,
			controlTypes.Role.LIST,
			self.CONTACT_LIST_UIA_CLASS,
			self.CONTACT_LIST_UIA_ID,
		)

	def _findSearchEdit(self) -> UIAObject | None:
		"""Find the search edit field in the main window."""
		return self._findMainWindowDescendant(
			UIA.UIA_EditControlTypeId,
			controlTypes.Role.EDITABLETEXT,
			self.SEARCH_EDIT_UIA_CLASS,
		)

	def _findVoipTrayWindow(self) -> UIAObject | None:
		"""Find the audio/video call tray window beside the foreground window."""
		return self._findForegroundSibling(
			controlTypes.Role.WINDOW,
			self.VOIP_TRAY_WINDOW_UIA_CLASS,
			self.VOIP_TRAY_WINDOW_UIA_ID,
		)

	def _getCurrentChatIdentity(self, focus: UIAObject) -> ChatIdentity | None:
		foreground = api.getForegroundObject()
		if not isinstance(foreground, UIAObject):
			return None
		foregroundClassName = foreground.UIAElement.CachedClassName
		if foregroundClassName == self.SINGLE_CHAT_WINDOW_UIA_CLASS:
			automationId = foreground.UIAAutomationId
			if automationId:
				return "single", automationId
			return None
		if foregroundClassName == self.MAIN_WINDOW_UIA_CLASS:
			name = focus.name
			if name and not speech.isBlank(name):
				return "main", name
		return None

	def _updateActiveChatFromInput(self, focus: UIAObject) -> None:
		chatIdentity = self._getCurrentChatIdentity(focus)
		if not chatIdentity or chatIdentity == self.activeChatIdentity:
			return
		if (
			self.activeChatIdentity is not None
			or self.reviewState.messages
			or self.lastNotifiedMessageRecord is not None
			or self.activeMessageList is not None
		):
			self._cancelPendingBoundaryReview()
			self.reviewState = ReviewState(messages=[])
			self.lastNotifiedMessageRecord = None
			self.activeMessageList = None
		self.activeChatIdentity = chatIdentity

	def _getUIAMessageRecord(self, element: UIA.IUIAutomationElement) -> MessageRecord | None:
		try:
			controlType = element.getCachedPropertyValue(UIA.UIA_ControlTypePropertyId)
			automationId = element.getCachedPropertyValue(UIA.UIA_AutomationIdPropertyId)
			className = element.getCachedPropertyValue(UIA.UIA_ClassNamePropertyId)
			text = element.getCachedPropertyValue(UIA.UIA_NamePropertyId)
		except Exception:
			return None
		notSupportedValue = UIAHandler.handler.reservedNotSupportedValue
		if controlType == notSupportedValue:
			return None
		if controlType != UIA.UIA_ListItemControlTypeId:
			return None
		if automationId == notSupportedValue:
			automationId = None
		if className == notSupportedValue:
			className = None
		if automationId != self.MESSAGE_ITEM_UIA_ID and className != self.MESSAGE_TIME_ITEM_UIA_CLASS:
			return None
		if text == notSupportedValue:
			text = None
		if not text or speech.isBlank(text):
			return None
		identity = self._getMessageIdentityFromElement(element)
		if identity is None:
			return None
		return MessageRecord(
			identity=identity,
			text=text,
		)

	def _getMessageIdentityFromElement(self, element: UIA.IUIAutomationElement) -> MessageIdentity | None:
		"""Return a RuntimeId-based identity for a UIA message element."""
		try:
			runtimeId = tuple(int(value) for value in element.getRuntimeId())
		except Exception:
			return None
		if not runtimeId:
			return None
		return "uiaRuntime", runtimeId

	def _getMessageRecordFromObject(self, obj: UIAObject) -> MessageRecord | None:
		"""Return a message record from a live UIA message object."""
		automationId = obj.UIAAutomationId
		className = obj.UIAElement.CachedClassName
		if obj.role != controlTypes.Role.LISTITEM or (
			automationId != self.MESSAGE_ITEM_UIA_ID and className != self.MESSAGE_TIME_ITEM_UIA_CLASS
		):
			return None
		text = obj.name
		if not text or speech.isBlank(text):
			return None
		identity = self._getMessageIdentityFromElement(obj.UIAElement)
		if identity is None:
			return None
		return MessageRecord(
			identity=identity,
			text=text,
		)

	def _collectVisibleMessageRecords(
		self,
		messageList: UIAObject,
	) -> list[MessageRecord]:
		messageListElement = messageList.UIAElement
		try:
			childrenCacheRequest = UIAHandler.handler.baseCacheRequest.clone()
			childrenCacheRequest.addProperty(UIA.UIA_ControlTypePropertyId)
			childrenCacheRequest.addProperty(UIA.UIA_NamePropertyId)
			childrenCacheRequest.addProperty(UIA.UIA_AutomationIdPropertyId)
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

	def _doMessageRecordListsMatch(self, source: list[MessageRecord], target: list[MessageRecord]) -> bool:
		"""Return whether two message lists have the same RuntimeId sequence."""
		if len(source) != len(target):
			return False
		return all(
			sourceRecord.identity == targetRecord.identity
			for sourceRecord, targetRecord in zip(source, target)
		)

	def _findSubList(self, source: list[MessageRecord], target: list[MessageRecord]) -> int | None:
		if not target or len(target) > len(source):
			return None
		for index in range(len(source) - len(target) + 1):
			if self._doMessageRecordListsMatch(source[index : index + len(target)], target):
				return index
		return None

	def _getSuffixPrefixOverlap(self, left: list[MessageRecord], right: list[MessageRecord]) -> int:
		maxOverlap = min(len(left), len(right))
		for count in range(maxOverlap, 0, -1):
			if self._doMessageRecordListsMatch(left[-count:], right[:count]):
				return count
		return 0

	def _mergeVisibleMessages(
		self,
		state: ReviewState,
		visibleMessages: list[MessageRecord],
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
		visibleStartIndex = self._findSubList(oldMessages, visibleMessages)
		if visibleStartIndex is not None:
			return self.QUEUE_UPDATE_UNCHANGED
		oldStartIndex = self._findSubList(visibleMessages, oldMessages)
		if oldStartIndex is not None:
			newMessages = visibleMessages
			indexOffset = oldStartIndex
			if len(visibleMessages) > len(oldMessages):
				if oldStartIndex == 0:
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

	def _trimReviewState(self, state: ReviewState) -> None:
		overflow = len(state.messages) - self.MAX_MESSAGE_QUEUE_SIZE
		if overflow <= 0:
			return
		del state.messages[:overflow]
		state.currentIndex = max(0, state.currentIndex - overflow)

	def refreshMessageQueue(
		self,
		messageList: UIAObject | None = None,
		setNotificationBaseline: bool = False,
	) -> ReviewState:
		self.reviewQueueUpdateOnLastRefresh = self.QUEUE_UPDATE_UNCHANGED
		focus = api.getFocusObject()
		if isinstance(focus, UIAObject) and focus.UIAAutomationId == self.MESSAGE_INPUT_UIA_ID:
			self._updateActiveChatFromInput(focus)
			if messageList is None:
				messageList = self._getCurrentMessageList(focus)
		if messageList is None:
			return self.reviewState
		if not isinstance(messageList, UIAObject):
			return self.reviewState
		self.activeMessageList = messageList
		records = self._collectVisibleMessageRecords(messageList)
		if not records:
			return self.reviewState
		state = self.reviewState
		self.reviewQueueUpdateOnLastRefresh = self._mergeVisibleMessages(state, records)
		if setNotificationBaseline:
			self.lastNotifiedMessageRecord = records[-1]
		return state

	def _getPressedAltKeys(self) -> list[PressedAltKey]:
		pressedKeys = []
		altKeyFlags = (
			(winUser.VK_LMENU, 0),
			(winUser.VK_RMENU, self.KEYEVENTF_EXTENDEDKEY),
		)
		for vkCode, flags in altKeyFlags:
			if winUser.getAsyncKeyState(vkCode) & 32768:
				pressedKeys.append((vkCode, flags))
		if not pressedKeys and winUser.getAsyncKeyState(winUser.VK_MENU) & 32768:
			pressedKeys.append((winUser.VK_MENU, 0))
		return pressedKeys

	def _setSyntheticAltKeysState(self, pressedKeys: list[PressedAltKey], isKeyUp: bool) -> None:
		keyUpFlag = self.KEYEVENTF_KEYUP if isKeyUp else 0
		for vkCode, flags in pressedKeys:
			winUser.keybd_event(vkCode, 0, flags | keyUpFlag, 0)

	def _scrollMessageList(self, messageList: UIAObject, scrollSteps: int) -> bool:
		"""Scroll the message list by moving the mouse to the list temporarily."""
		left, top, width, height = messageList.location
		if width <= 0 or height <= 0:
			return False
		point = int(left + width / 2), int(top + height / 2)
		oldX, oldY = winUser.getCursorPos()
		pressedAltKeys = self._getPressedAltKeys()
		try:
			if pressedAltKeys:
				self._setSyntheticAltKeysState(pressedAltKeys, True)
			winUser.setCursorPos(*point)
			mouseHandler.scrollMouseWheel(scrollSteps, isVertical=True)
		except Exception:
			log.debugWarning("Unable to scroll the WeChat message list.", exc_info=True)
			return False
		finally:
			if pressedAltKeys:
				try:
					self._setSyntheticAltKeysState(list(reversed(pressedAltKeys)), False)
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
		ui.message(state.messages[index].text)
		return True

	def _getPreviousBoundaryTargetIndex(
		self,
		state: ReviewState,
		oldMessages: list[MessageRecord],
	) -> int | None:
		oldMessagesStartIndex = self._findSubList(state.messages, oldMessages)
		if oldMessagesStartIndex is not None and oldMessagesStartIndex > 0:
			return oldMessagesStartIndex - 1
		return None

	def _readAfterPreviousBoundaryScroll(
		self,
		oldMessages: list[MessageRecord],
		oldIndex: int,
		nextAttempt: int,
	) -> None:
		"""Refresh after a boundary scroll and read the requested relative message."""
		scheduledRetry = False
		try:
			if not self._isMessageInputFocus():
				return
			state = self.refreshMessageQueue(setNotificationBaseline=True)
			if not state.messages:
				return
			targetIndex = self._getPreviousBoundaryTargetIndex(state, oldMessages)
			if targetIndex is not None:
				if self._speakMessageAtIndex(state, targetIndex):
					return
			if nextAttempt <= self.MAX_BOUNDARY_SCROLL_ATTEMPTS:
				scheduledRetry = True
				self._scrollPreviousBoundaryAndRead(state, attempt=nextAttempt)
				return

			oldMessagesStartIndex = self._findSubList(state.messages, oldMessages)
			if oldMessagesStartIndex is not None:
				state.currentIndex = oldMessagesStartIndex + oldIndex
			return
		finally:
			if not scheduledRetry:
				self.isBoundaryScrollPending = False

	def _scrollPreviousBoundaryAndRead(self, state: ReviewState, attempt: int = 0) -> None:
		"""Scroll above the visible boundary and read after WeChat updates the list."""
		self._suppressNotificationsForUserAction()
		messageList = self._getCurrentMessageList()
		if messageList is None:
			self.isBoundaryScrollPending = False
			return
		oldMessages = list(state.messages)
		oldIndex = state.currentIndex
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
		self.isBoundaryScrollPending = True
		if attempt >= 2:
			wheelUnits = 8
		elif attempt >= 1:
			wheelUnits = 6
		else:
			wheelUnits = 4
		scrollSteps = winUser.WHEEL_DELTA * wheelUnits

		if not self._scrollMessageList(messageList, scrollSteps):
			if attempt < self.MAX_BOUNDARY_SCROLL_ATTEMPTS:
				self._scrollPreviousBoundaryAndRead(state, attempt=attempt + 1)
				return
			self.isBoundaryScrollPending = False
			return

		self.scrollLoadTimer = wx.CallLater(
			self.SCROLL_LOAD_DELAY,
			self._readAfterPreviousBoundaryScroll,
			oldMessages,
			oldIndex,
			attempt + 1,
		)

	def _getMessageListFromScrollbar(self, obj: UIAObject) -> UIAObject | None:
		"""Return the message list parent for a WeChat message list scrollbar."""
		parent = obj.parent
		if (
			obj.role != controlTypes.Role.SCROLLBAR
			or not isinstance(parent, UIAObject)
			or parent.UIAAutomationId != self.MESSAGE_LIST_UIA_ID
		):
			return None
		return parent

	def _handleValueChange(self, obj: NVDAObject) -> None:
		"""Handle WeChat message list value changes."""
		if not isinstance(obj, UIAObject):
			return
		messageList = self._getMessageListFromScrollbar(obj)
		if messageList is None:
			return
		if (
			self.notificationMode == self.MODE_OFF
			or self.isNotificationSuppressed
			or self.isBoundaryScrollPending
		):
			return
		latestMessage = messageList.lastChild
		if not isinstance(latestMessage, UIAObject):
			return
		messageRecord = self._getMessageRecordFromObject(latestMessage)
		if messageRecord is None:
			return
		if (
			self.lastNotifiedMessageRecord is not None
			and messageRecord.identity == self.lastNotifiedMessageRecord.identity
		):
			return
		self.refreshMessageQueue(messageList)
		queueUpdateKind = self.reviewQueueUpdateOnLastRefresh
		if queueUpdateKind == self.QUEUE_UPDATE_APPEND:
			playWaveFile(self.SOUND_NEW_MESSAGE)
			if self.notificationMode == self.MODE_SOUND_AND_SPEECH:
				ui.message(messageRecord.text)
		self.lastNotifiedMessageRecord = messageRecord

	def event_valueChange(self, obj: NVDAObject, nextHandler: NextHandler) -> None:
		"""Handle value changes without interrupting the NVDA event chain."""
		try:
			self._handleValueChange(obj)
		except Exception:
			log.debugWarning("Unable to handle a WeChat value change event.", exc_info=True)
		nextHandler()

	def _getMessageListFromFocusedItem(self, obj: UIAObject) -> UIAObject | None:
		"""Return the message list parent for a focused WeChat message item."""
		parent = obj.parent
		if (
			obj.role != controlTypes.Role.LISTITEM
			or not isinstance(parent, UIAObject)
			or parent.UIAAutomationId != self.MESSAGE_LIST_UIA_ID
		):
			return None
		if (
			obj.UIAAutomationId == self.MESSAGE_ITEM_UIA_ID
			or obj.UIAElement.CachedClassName == self.MESSAGE_TIME_ITEM_UIA_CLASS
		):
			return parent
		return None

	def _handleGainFocus(self, obj: NVDAObject) -> None:
		"""Refresh WeChat review state for relevant focus changes."""
		if not isinstance(obj, UIAObject):
			return
		messageList = self._getMessageListFromFocusedItem(obj)
		if messageList is not None:
			self._suppressNotificationsForUserAction()
			self.refreshMessageQueue(messageList, setNotificationBaseline=True)
			return
		automationId = obj.UIAAutomationId
		if automationId == self.MESSAGE_LIST_UIA_ID:
			self.refreshMessageQueue(obj, setNotificationBaseline=True)
		elif automationId == self.MESSAGE_INPUT_UIA_ID:
			self.refreshMessageQueue(setNotificationBaseline=True)

	def event_gainFocus(self, obj: NVDAObject, nextHandler: NextHandler) -> None:
		"""Handle focus gains without interrupting the NVDA event chain."""
		try:
			self._handleGainFocus(obj)
		except Exception:
			log.debugWarning("Unable to handle a WeChat gain focus event.", exc_info=True)
		nextHandler()

	def _suppressNotificationsForUserAction(self) -> None:
		self.isNotificationSuppressed = True
		if self.notificationSuppressionTimer and self.notificationSuppressionTimer.IsRunning():
			self.notificationSuppressionTimer.Restart(self.NOTIFICATION_SUPPRESSION_DELAY)
		else:
			self.notificationSuppressionTimer = wx.CallLater(
				self.NOTIFICATION_SUPPRESSION_DELAY,
				self._clearNotificationSuppression,
			)

	def _clearNotificationSuppression(self) -> None:
		self.isNotificationSuppressed = False

	def _isMessageInputFocus(self) -> bool:
		focus = api.getFocusObject()
		return isinstance(focus, UIAObject) and focus.UIAAutomationId == self.MESSAGE_INPUT_UIA_ID

	def _cancelPendingBoundaryReview(self) -> None:
		if self.scrollLoadTimer and self.scrollLoadTimer.IsRunning():
			self.scrollLoadTimer.Stop()
		self.scrollLoadTimer = None
		self.isBoundaryScrollPending = False

	def _sendReviewGestureThrough(self, gesture: inputCore.InputGesture) -> None:
		self._cancelPendingBoundaryReview()
		gesture.send()

	def _readReviewDirection(self, state: ReviewState, direction: int) -> bool:
		if direction > 0:
			state = self.refreshMessageQueue(setNotificationBaseline=True)
		nextIndex = state.currentIndex + direction
		if nextIndex < 0:
			self._scrollPreviousBoundaryAndRead(state)
			return False
		if nextIndex >= len(state.messages):
			return self._speakMessageAtIndex(state, state.currentIndex)
		return self._speakMessageAtIndex(state, nextIndex)

	def _getActiveReviewState(self) -> ReviewState | None:
		focus = api.getFocusObject()
		if isinstance(focus, UIAObject) and focus.UIAAutomationId == self.MESSAGE_INPUT_UIA_ID:
			self._updateActiveChatFromInput(focus)
		state = self.reviewState
		if not state.messages:
			state = self.refreshMessageQueue(setNotificationBaseline=True)
		if state.messages:
			return state
		ui.message(self.NO_MESSAGES_TEXT)
		return None

	def _handleRelativeReviewGesture(
		self,
		gesture: inputCore.InputGesture,
		direction: int,
	) -> None:
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

	def _readIndexedReviewMessage(self, gesture: inputCore.InputGesture, index: int) -> None:
		if not self._isMessageInputFocus():
			self._sendReviewGestureThrough(gesture)
			return
		self._suppressNotificationsForUserAction()
		self._cancelPendingBoundaryReview()
		state = self._getActiveReviewState()
		if state is None:
			return
		if index < 0:
			oldMessages = list(state.messages)
			oldIndex = state.currentIndex
			state = self.refreshMessageQueue(setNotificationBaseline=True)
			if self.reviewQueueUpdateOnLastRefresh == self.QUEUE_UPDATE_REPLACE and oldMessages:
				state.messages = oldMessages
				state.currentIndex = oldIndex
				self.reviewQueueUpdateOnLastRefresh = self.QUEUE_UPDATE_UNCHANGED
			index = len(state.messages) + index
		self._speakMessageAtIndex(state, index)

	def _focusObjectOrSendGesture(
		self,
		obj: UIAObject | None,
		gesture: inputCore.InputGesture,
		logMessage: str,
	) -> None:
		"""Focus an object, falling back to the original gesture."""
		if obj is None:
			gesture.send()
			return
		try:
			obj.setFocus()
		except Exception:
			log.debugWarning(logMessage, exc_info=True)
			gesture.send()
			return
		self._cancelPendingBoundaryReview()
		api.setNavigatorObject(obj, True)

	@script(
		# Translators: Description for the command that moves focus to the WeChat Official Accounts list.
		description=_("Moves focus to the WeChat Official Accounts list"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+p",
	)
	def script_focusOfficialAccountList(self, gesture: inputCore.InputGesture) -> None:
		"""Move focus to the Official Accounts list in the WeChat main window."""
		self._focusObjectOrSendGesture(
			self._findOfficialAccountList(),
			gesture,
			"Unable to focus the WeChat Official Accounts list.",
		)

	@script(
		# Translators: Description for the command that moves focus to the WeChat Contacts list.
		description=_("Moves focus to the WeChat Contacts list"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+t",
	)
	def script_focusContactList(self, gesture: inputCore.InputGesture) -> None:
		"""Move focus to the Contacts list in the WeChat main window."""
		self._focusObjectOrSendGesture(
			self._findContactList(),
			gesture,
			"Unable to focus the WeChat Contacts list.",
		)

	@script(
		# Translators: Description for the command that moves focus to the WeChat search field.
		description=_("Moves focus to the WeChat search field"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+s",
	)
	def script_focusSearchEdit(self, gesture: inputCore.InputGesture) -> None:
		"""Move focus to the search edit field in the WeChat main window."""
		self._focusObjectOrSendGesture(
			self._findSearchEdit(),
			gesture,
			"Unable to focus the WeChat search field.",
		)

	@script(
		# Translators: Description for the command that moves focus to the WeChat audio/video call tray window.
		description=_("Moves focus to the WeChat audio/video call tray window"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+v",
	)
	def script_focusVoipTrayWindow(self, gesture: inputCore.InputGesture) -> None:
		"""Move focus to the audio/video call tray window."""
		self._focusObjectOrSendGesture(
			self._findVoipTrayWindow(),
			gesture,
			"Unable to focus the WeChat audio/video call tray window.",
		)

	@script(
		# Translators: Description for the command that reads the previous WeChat message.
		description=_("Reads the previous message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+Control+upArrow",
	)
	def script_readPreviousMessage(self, gesture: inputCore.InputGesture) -> None:
		self._handleRelativeReviewGesture(gesture, -1)

	@script(
		# Translators: Description for the command that reads the next WeChat message.
		description=_("Reads the next message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+Control+downArrow",
	)
	def script_readNextMessage(self, gesture: inputCore.InputGesture) -> None:
		self._handleRelativeReviewGesture(gesture, 1)

	@script(
		# Translators: Description for the command that reads the first WeChat message.
		description=_("Reads the first message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+Control+home",
	)
	def script_readFirstMessage(self, gesture: inputCore.InputGesture) -> None:
		self._readIndexedReviewMessage(gesture, 0)

	@script(
		# Translators: Description for the command that reads the last WeChat message.
		description=_("Reads the last message in the current WeChat chat"),
		category=SCRIPT_CATEGORY,
		gesture="kb:alt+Control+end",
	)
	def script_readLastMessage(self, gesture: inputCore.InputGesture) -> None:
		self._readIndexedReviewMessage(gesture, -1)

	@script(
		# Translators: Description for the command that changes new message notification mode.
		description=_(
			"Cycles through new message notification modes (Off -> Sound Only -> Sound and Speech)",
		),
		category=SCRIPT_CATEGORY,
		gesture="kb:f3",
	)
	def script_toggleNotificationMode(self, gesture: inputCore.InputGesture) -> None:
		self.notificationMode = (self.notificationMode + 1) % 3
		config.conf[self.CONFIG_SECTION][self.CONFIG_KEY_NOTIFICATION_MODE] = self.notificationMode
		modeMessages = (
			_("Off"),
			_("Sound Only"),
			_("Sound and speech"),
		)
		ui.message(modeMessages[self.notificationMode])
		self.refreshMessageQueue(setNotificationBaseline=True)
