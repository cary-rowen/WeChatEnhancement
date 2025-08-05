
import api
import appModuleHandler
import controlTypes
import eventHandler
import speech
from versionInfo import version_year

role = controlTypes.Role if version_year >= 2022 else controlTypes.role.Role

class AppModule(appModuleHandler.AppModule):

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		eventHandler.requestEvents("gainFocus", self.processID, "Qt51514QWindowToolSaveBits") 

	def event_gainFocus(self, obj, nextHandler, isFocus=False):
		if obj.role == role.TOOLBAR and obj.UIAAutomationId == "main_tabbar":
			nextHandler()
			speech.speakObject(obj.simpleFirstChild)
		nextHandler()
