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

