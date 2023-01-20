import appModuleHandler
import api
import ui
import mouseHandler
import winUser
from scriptHandler import script
import controlTypes
from versionInfo import version_year

role = controlTypes.Role if version_year>=2022 else controlTypes.role.Role


class AppModule(appModuleHandler.AppModule):

	def event_gainFocus(self, obj, nextHandler, isFocus=False):
		try:
			if obj.treeInterceptor and obj.role==role.LIST and "discuss_list" == obj.IA2Attributes.get("class"):
				o=obj.firstChild.firstChild
				while o:
					ui.message(o.firstChild.name)
					o=o.next
			elif obj.treeInterceptor and obj.role==role.LISTITEM and "js_comment" in obj.IA2Attributes.get("class"):
				ui.message(obj.firstChild.name)
			elif obj.treeInterceptor and obj.role==role.BUTTON and "sns_opr_btn sns_praise_btn" == obj.IA2Attributes.get("class"):
				ui.message(obj.simplePrevious.name)
		except: pass
		nextHandler()


	@script(
		description="后退到上一页",
		category="PC微信增强",
		gesture="kb:alt+leftArrow"
	)
	def script_back(self,gesture):
		self.clickButton("后退")


	@script(
		description="关闭窗口",
		category="PC微信增强",
		gesture="kb:control+w"
	)
	def script_close(self,gesture):
		self.clickButton("关闭")

	def clickButton(self, name):
		obj = api.getForegroundObject()
		if not obj:
			return
		for child in obj.recursiveDescendants:
			if child.role == role.BUTTON and child.name == name:
				self.click(child)
	def click(self, obj):
		l, t, w, h = obj.location
		x, y = int(l + w / 2), int(t + h / 2)
		winUser.setCursorPos(x, y)
		mouseHandler.executeMouseMoveEvent(x, y)
		mouseHandler.doPrimaryClick()
