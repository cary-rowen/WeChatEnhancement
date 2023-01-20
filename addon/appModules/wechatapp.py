import appModuleHandler
import api
import ui
from scriptHandler import script

class AppModule(appModuleHandler.AppModule):

	@script(
		description="后退到上一页",
		category="PC微信增强",
		gesture="kb:alt+leftArrow"
	)
	def script_back(self,gesture):
		o=api.getForegroundObject().simpleFirstChild
		if 0X400 in o.states or 0x400000 in o.states:
			ui.message(u"无法返回")
			return
		if o.name=="后退":
			o.doAction()
		else:
			ui.message(o.name)

	@script(
		description="关闭窗口",
		category="PC微信增强",
		gesture="kb:Control+W"
	)
	def script_exit(self,gesture):
		o=api.getForegroundObject().simpleFirstChild
		while o.name!="关闭":
			o=o.simpleNext
		o.doAction()
