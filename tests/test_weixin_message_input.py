"""Regression tests for WeChat message-input text ranges."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types
import unittest


class _COMError(Exception):
	pass


class _UIATextInfo:
	def expand(self, unit: str) -> None:
		raise self.baseExpandError


class _UIAObject:
	def event_mouseMove(self, x: int, y: int) -> None:
		raise self.baseMouseMoveError


def _installStubs() -> None:
	textInfos = types.ModuleType("textInfos")
	textInfos.UNIT_CHARACTER = "character"
	textInfos.UNIT_WORD = "word"
	textInfos.UNIT_LINE = "line"
	textInfos.UNIT_PARAGRAPH = "paragraph"
	textInfos.TextInfo = object
	textOffsets = types.ModuleType("textInfos.offsets")
	textOffsets.findStartOfWord = lambda text, offset: offset
	textOffsets.findEndOfWord = lambda text, offset: offset
	textInfos.offsets = textOffsets

	UIAHandler = types.ModuleType("UIAHandler")
	UIAHandler.TextPatternRangeEndpoint_Start = 0
	UIAHandler.TextPatternRangeEndpoint_End = 1
	UIAHandler.TextUnit_Character = 0

	comtypes = types.ModuleType("comtypes")
	comtypes.COMError = _COMError

	NVDAObjects = types.ModuleType("NVDAObjects")
	NVDAObjectsUIA = types.ModuleType("NVDAObjects.UIA")
	NVDAObjectsUIA.UIA = _UIAObject
	NVDAObjectsUIA.UIATextInfo = _UIATextInfo
	NVDAObjects.UIA = NVDAObjectsUIA

	sys.modules.update(
		{
			"textInfos": textInfos,
			"textInfos.offsets": textOffsets,
			"UIAHandler": UIAHandler,
			"comtypes": comtypes,
			"NVDAObjects": NVDAObjects,
			"NVDAObjects.UIA": NVDAObjectsUIA,
		}
	)


def _loadModule():
	_installStubs()
	path = pathlib.Path(__file__).parents[1] / "addon" / "appModules" / "_weixinMessageInput.py"
	spec = importlib.util.spec_from_file_location("weixin_message_input_under_test", path)
	assert spec is not None and spec.loader is not None
	module = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(module)
	return module


weixinMessageInput = _loadModule()


class ExpandTests(unittest.TestCase):
	def _textInfoWithBaseError(self, error: BaseException):
		info = object.__new__(weixinMessageInput.WeChatMessageInputTextInfo)
		info.baseExpandError = error
		return info

	def test_null_com_pointer_value_error_is_ignored(self):
		info = self._textInfoWithBaseError(ValueError("NULL COM pointer access"))
		info.expand("paragraph")

	def test_com_error_is_ignored(self):
		info = self._textInfoWithBaseError(_COMError("stale range"))
		info.expand("paragraph")

	def test_unrelated_value_error_is_not_hidden(self):
		info = self._textInfoWithBaseError(ValueError("unrelated failure"))
		with self.assertRaisesRegex(ValueError, "unrelated failure"):
			info.expand("paragraph")


class MouseMoveTests(unittest.TestCase):
	def _inputWithBaseError(self, error: BaseException):
		obj = object.__new__(weixinMessageInput.WeChatMessageInput)
		obj.baseMouseMoveError = error
		return obj

	def test_null_com_pointer_value_error_is_ignored(self):
		obj = self._inputWithBaseError(ValueError("NULL COM pointer access"))
		obj.event_mouseMove(10, 20)

	def test_com_error_is_ignored(self):
		obj = self._inputWithBaseError(_COMError("stale range"))
		obj.event_mouseMove(10, 20)

	def test_unrelated_value_error_is_not_hidden(self):
		obj = self._inputWithBaseError(ValueError("unrelated failure"))
		with self.assertRaisesRegex(ValueError, "unrelated failure"):
			obj.event_mouseMove(10, 20)


if __name__ == "__main__":
	unittest.main()
