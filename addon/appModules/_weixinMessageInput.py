# -*- coding: utf-8 -*-
# PC WeChat add-on for NVDA
# This file is covered by the GNU General Public License.
# See the file COPYING.txt for more details.
# Copyright (C) 2025 Cary-rowen <manchen_0528@outlook.com>

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING, Any, cast

import textInfos
import UIAHandler
from comtypes import COMError
from textInfos.offsets import findEndOfWord, findStartOfWord

from NVDAObjects import NVDAObject
from NVDAObjects.UIA import UIA as UIAObject
from NVDAObjects.UIA import UIATextInfo

if TYPE_CHECKING:
	import inputCore
	from typing import override

else:

	def override(method: Any) -> Any:
		"""Return overridden methods unchanged at runtime."""
		return method


class WeChatMessageInputTextInfo(UIATextInfo):
	"""UIA text info for WeChat message input caret navigation."""

	_TEXT_TRANSLATION = str.maketrans(
		{
			"\u2028": "\n",
			"\u2029": "\n",
		},
	)
	"""Text normalization for line separators exposed by WeChat."""

	_isAtEndOfText = False
	"""Whether this range is the final collapsed insertion point."""

	def __init__(self, obj: NVDAObject, position: Any, _rangeObj: Any = None) -> None:
		"""Initialize range state for caret navigation."""
		super().__init__(obj, position, _rangeObj)
		self._isAtEndOfText = position == textInfos.POSITION_CARET and self._isAtBrokenDocumentEnd()

	def _isAtBrokenDocumentEnd(self) -> bool:
		"""Return whether the final insertion point expands to the previous character."""
		try:
			rangeObj: Any = getattr(self, "_rangeObj")
			if (
				rangeObj.CompareEndpoints(
					UIAHandler.TextPatternRangeEndpoint_Start,
					rangeObj,
					UIAHandler.TextPatternRangeEndpoint_End,
				)
				!= 0
			):
				return False
			textPattern: Any = getattr(self.obj, "UIATextPattern")
			documentRange: Any = textPattern.documentRange
			if (
				rangeObj.CompareEndpoints(
					UIAHandler.TextPatternRangeEndpoint_Start,
					documentRange,
					UIAHandler.TextPatternRangeEndpoint_End,
				)
				< 0
			):
				return False
			tempRange: Any = rangeObj.clone()
			tempRange.ExpandToEnclosingUnit(UIAHandler.TextUnit_Character)
			return (
				cast(
					int,
					rangeObj.CompareEndpoints(
						UIAHandler.TextPatternRangeEndpoint_Start,
						tempRange,
						UIAHandler.TextPatternRangeEndpoint_Start,
					),
				)
				> 0
			)
		except (AttributeError, COMError):
			return False

	@override
	def _getTextFromUIARange(self, textRange: Any) -> str:
		"""Return text with WeChat line separators normalized for NVDA speech."""
		text = super()._getTextFromUIARange(textRange)
		return text.translate(self._TEXT_TRANSLATION)

	def _getDocumentTextAndRangeStartOffset(self) -> tuple[str, int]:
		"""Return the document text and this range's start offset within it."""
		documentText, startOffset, _endOffset = self._getDocumentTextAndRangeOffsets()
		return documentText, startOffset

	def _getDocumentTextAndRangeOffsets(self) -> tuple[str, int, int]:
		"""Return the document text and this range's offsets within it."""
		textPattern: Any = getattr(self.obj, "UIATextPattern")
		documentRange: Any = textPattern.documentRange
		documentText = self._getTextFromUIARange(documentRange)
		rangeObj: Any = getattr(self, "_rangeObj")
		startOffset = self._getOffsetForRangeEndpoint(
			documentRange,
			rangeObj,
			UIAHandler.TextPatternRangeEndpoint_Start,
			len(documentText),
		)
		endOffset = self._getOffsetForRangeEndpoint(
			documentRange,
			rangeObj,
			UIAHandler.TextPatternRangeEndpoint_End,
			len(documentText),
		)
		return documentText, startOffset, endOffset

	def _getOffsetForRangeEndpoint(
		self,
		documentRange: Any,
		rangeObj: Any,
		endpoint: int,
		documentLength: int,
	) -> int:
		"""Return a document-relative text offset for a UIA range endpoint."""
		prefixRange: Any = documentRange.clone()
		prefixRange.MoveEndpointByRange(
			UIAHandler.TextPatternRangeEndpoint_End,
			rangeObj,
			endpoint,
		)
		prefixText = self._getTextFromUIARange(prefixRange)
		return min(max(len(prefixText), 0), documentLength)

	@classmethod
	def _getLineOffsetEntries(cls, text: str) -> list[tuple[int, int, int]]:
		"""Return logical line start, content end, and next start offsets."""
		entries: list[tuple[int, int, int]] = []
		lineStart = 0
		for line in text.splitlines(keepends=True):
			lineEnd = lineStart + len(line)
			if line.endswith("\r\n"):
				lineContentEnd = lineEnd - 2
			elif line.endswith(("\n", "\r")):
				lineContentEnd = lineEnd - 1
			else:
				lineContentEnd = lineEnd
			entries.append((lineStart, lineContentEnd, lineEnd))
			lineStart = lineEnd
		if not entries or text.endswith(("\n", "\r")):
			entries.append((lineStart, len(text), len(text)))
		return entries

	@classmethod
	def _getLineIndexAtOffset(cls, entries: list[tuple[int, int, int]], offset: int) -> int:
		"""Return the logical line index containing an offset."""
		for index, (_lineStart, lineContentEnd, nextLineStart) in enumerate(entries):
			if offset < nextLineStart or offset == lineContentEnd:
				return index
		return len(entries) - 1

	@classmethod
	def _getLineOffsets(cls, text: str, offset: int) -> tuple[int, int]:
		"""Return the current line's start and end offsets."""
		offset = min(max(offset, 0), len(text))
		entries = cls._getLineOffsetEntries(text)
		lineIndex = cls._getLineIndexAtOffset(entries, offset)
		lineStart, lineContentEnd, _nextLineStart = entries[lineIndex]
		return lineStart, lineContentEnd

	@classmethod
	def _getWordOffsets(cls, text: str, offset: int) -> tuple[int, int]:
		"""Return the current logical word's start and end offsets."""
		if not text:
			return 0, 0
		offset = min(max(offset, 0), len(text))
		if offset >= len(text):
			return len(text), len(text)
		lineStart, lineEnd = cls._getLineOffsets(text, offset)
		if lineStart == lineEnd:
			return lineStart, lineEnd
		lineText = text[lineStart:lineEnd].translate({0: " ", 0xA0: " "})
		relativeOffset = min(max(offset - lineStart, 0), len(lineText) - 1)
		wordStart = findStartOfWord(lineText, relativeOffset)
		wordEnd = findEndOfWord(lineText, relativeOffset)
		return lineStart + wordStart, lineStart + min(wordEnd, len(lineText))

	@staticmethod
	def _compareOffsets(firstOffset: int, secondOffset: int) -> int:
		"""Compare two document offsets using TextInfo endpoint semantics."""
		if firstOffset < secondOffset:
			return -1
		if firstOffset > secondOffset:
			return 1
		return 0

	def _setRangeFromDocumentOffsets(self, startOffset: int, endOffset: int) -> None:
		"""Set this range to document-relative text offsets."""
		textPattern: Any = getattr(self.obj, "UIATextPattern")
		rangeObj: Any = textPattern.documentRange.clone()
		rangeObj.MoveEndpointByRange(
			UIAHandler.TextPatternRangeEndpoint_End,
			rangeObj,
			UIAHandler.TextPatternRangeEndpoint_Start,
		)
		rangeObj.MoveEndpointByUnit(
			UIAHandler.TextPatternRangeEndpoint_End,
			UIAHandler.TextUnit_Character,
			endOffset,
		)
		rangeObj.MoveEndpointByUnit(
			UIAHandler.TextPatternRangeEndpoint_Start,
			UIAHandler.TextUnit_Character,
			startOffset,
		)
		self._rangeObj = rangeObj

	def _moveByLogicalLine(self, direction: int) -> int:
		"""Move by logical lines derived from normalized document text."""
		documentText, startOffset, _endOffset = self._getDocumentTextAndRangeOffsets()
		entries = self._getLineOffsetEntries(documentText)
		currentIndex = self._getLineIndexAtOffset(entries, startOffset)
		targetIndex = min(max(currentIndex + direction, 0), len(entries) - 1)
		moved = targetIndex - currentIndex
		if moved == 0:
			return 0
		currentLineStart, currentLineEnd, _currentNextStart = entries[currentIndex]
		targetLineStart, targetLineEnd, _targetNextStart = entries[targetIndex]
		column = min(startOffset, currentLineEnd) - currentLineStart
		targetOffset = min(targetLineStart + column, targetLineEnd)
		self._setRangeFromDocumentOffsets(targetOffset, targetOffset)
		self._isAtEndOfText = targetOffset == len(documentText)
		return moved

	def _snapRedundantNativeWordBoundary(self, direction: int) -> bool:
		"""Snap from a native Qt word-end stop to the logical word boundary."""
		documentText, startOffset, endOffset = self._getDocumentTextAndRangeOffsets()
		if (
			direction == 0
			or startOffset != endOffset
			or startOffset <= 0
			or startOffset >= len(documentText)
			or not documentText[startOffset].isspace()
			or unicodedata.category(documentText[startOffset - 1])[0] not in "LMN"
		):
			return False
		if direction > 0:
			targetOffset = startOffset
			while targetOffset < len(documentText) and documentText[targetOffset].isspace():
				targetOffset += 1
		else:
			targetOffset, _wordEnd = self._getWordOffsets(documentText, startOffset - 1)
		if targetOffset == startOffset:
			return False
		self._setRangeFromDocumentOffsets(targetOffset, targetOffset)
		self._isAtEndOfText = targetOffset == len(documentText)
		return True

	def _expandToLine(self) -> bool:
		"""Expand to the current logical line, bypassing WeChat's broken UIA line unit."""
		try:
			documentText, startOffset = self._getDocumentTextAndRangeStartOffset()
			lineStart, lineEnd = self._getLineOffsets(documentText, startOffset)
			self._setRangeFromDocumentOffsets(lineStart, lineEnd)
			self._isAtEndOfText = lineStart == lineEnd == len(documentText)
			return True
		except (AttributeError, COMError):
			return False

	def _expandToWord(self) -> bool:
		"""Expand to the current logical word, bypassing WeChat's broken UIA word unit."""
		try:
			documentText, startOffset = self._getDocumentTextAndRangeStartOffset()
			wordStart, wordEnd = self._getWordOffsets(documentText, startOffset)
			self._setRangeFromDocumentOffsets(wordStart, wordEnd)
			self._isAtEndOfText = wordStart == wordEnd == len(documentText)
			return True
		except (AttributeError, COMError):
			return False

	@override
	def copy(self) -> "WeChatMessageInputTextInfo":
		"""Return a copy preserving the final insertion point state."""
		info = cast(WeChatMessageInputTextInfo, super().copy())
		info._isAtEndOfText = self._isAtEndOfText
		return info

	@override
	def _get_bookmark(self) -> Any:
		"""Return a bookmark based on logical document offsets."""
		try:
			_documentText, startOffset, endOffset = self._getDocumentTextAndRangeOffsets()
			return startOffset, endOffset
		except (AttributeError, COMError):
			return super()._get_bookmark()

	@override
	def expand(self, unit: str) -> None:
		"""Expand the range, keeping the final insertion point blank."""
		if unit in (textInfos.UNIT_CHARACTER, textInfos.UNIT_WORD) and self._isAtEndOfText:
			return
		if unit == textInfos.UNIT_WORD and self._expandToWord():
			return
		if unit == textInfos.UNIT_LINE and self._expandToLine():
			return
		self._isAtEndOfText = False
		super().expand(unit)

	@override
	def move(
		self,
		unit: str,
		direction: int,
		endPoint: str | None = None,
	) -> int:
		"""Move the range while preserving normal movement from the final insertion point."""
		if direction == 0:
			return 0
		if unit == textInfos.UNIT_LINE and endPoint is None:
			try:
				return self._moveByLogicalLine(direction)
			except (AttributeError, COMError):
				pass
		if self._isAtEndOfText and direction < 0:
			direction += 1
		self._isAtEndOfText = False
		if direction == 0:
			return -1
		return cast(int, super().move(unit, direction, endPoint=endPoint))

	@override
	def compareEndPoints(self, other: UIATextInfo, which: str) -> int:
		"""Compare endpoints using logical offsets for WeChat message input ranges."""
		if not isinstance(other, WeChatMessageInputTextInfo):
			return cast(int, super().compareEndPoints(other, which))
		try:
			_documentText, selfStartOffset, selfEndOffset = self._getDocumentTextAndRangeOffsets()
			_otherDocumentText, otherStartOffset, otherEndOffset = other._getDocumentTextAndRangeOffsets()
		except (AttributeError, COMError):
			return cast(int, super().compareEndPoints(other, which))
		selfEndPoint, otherEndPoint = which.split("To")
		if selfEndPoint == "start":
			selfOffset = selfStartOffset
		elif selfEndPoint == "end":
			selfOffset = selfEndOffset
		else:
			raise ValueError(f"bad argument - which: {which}")
		if otherEndPoint == "Start":
			otherOffset = otherStartOffset
		elif otherEndPoint == "End":
			otherOffset = otherEndOffset
		else:
			raise ValueError(f"bad argument - which: {which}")
		return self._compareOffsets(selfOffset, otherOffset)


class WeChatMessageInput(UIAObject):
	"""Overlay class for the WeChat chat message input field."""

	_TextInfo = WeChatMessageInputTextInfo
	_weChatCaretMovementUnit: str | None = None

	def _get_caretMovementDetectionUsesEvents(self) -> bool:
		"""Return False because WeChat Qt emits selection events for phantom line movement."""
		return False

	def _normalizeCaretAfterNativeWordMovement(
		self,
		oldStartOffset: int,
		newInfo: textInfos.TextInfo | None,
	) -> textInfos.TextInfo | None:
		"""Return a caret range corrected after WeChat's native word movement."""
		if not isinstance(newInfo, WeChatMessageInputTextInfo):
			return newInfo
		try:
			_documentText, newStartOffset = newInfo._getDocumentTextAndRangeStartOffset()
		except (AttributeError, COMError, RuntimeError, NotImplementedError):
			return newInfo
		if newStartOffset > oldStartOffset:
			direction = 1
		elif newStartOffset < oldStartOffset:
			direction = -1
		else:
			return newInfo
		correctedInfo = newInfo.copy()
		try:
			if correctedInfo._snapRedundantNativeWordBoundary(direction):
				correctedInfo.updateCaret()
				return correctedInfo
		except (AttributeError, COMError, RuntimeError, NotImplementedError):
			return newInfo
		return newInfo

	def _hasCaretMoved(
		self,
		bookmark: Any,
		retryInterval: float = 0.01,
		timeout: float | None = None,
		origWord: str | None = None,
	) -> tuple[bool, textInfos.TextInfo | None]:
		"""Return caret movement, correcting WeChat's native word stops when needed."""
		caretMoved, newInfo = super()._hasCaretMoved(
			bookmark,
			retryInterval=retryInterval,
			timeout=timeout,
			origWord=origWord,
		)
		if self._weChatCaretMovementUnit != textInfos.UNIT_WORD:
			return caretMoved, newInfo
		try:
			oldStartOffset, _oldEndOffset = bookmark
		except (TypeError, ValueError):
			return caretMoved, newInfo
		return caretMoved, self._normalizeCaretAfterNativeWordMovement(oldStartOffset, newInfo)

	def _caretMovementScriptHelper(self, gesture: "inputCore.InputGesture", unit: str) -> None:
		"""Run NVDA's standard caret movement helper with WeChat movement context."""
		oldUnit = self._weChatCaretMovementUnit
		self._weChatCaretMovementUnit = unit
		try:
			super()._caretMovementScriptHelper(gesture, unit)
		finally:
			self._weChatCaretMovementUnit = oldUnit
