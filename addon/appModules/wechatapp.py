import os,appModuleHandler, api,winUser,ui,eventHandler
from NVDAObjects import IAccessible

class AppModule(appModuleHandler.AppModule):

	def event_gainFocus(self, obj, nextHandler):
		if obj.role==3 or obj.role==1:
			o=api.getForegroundObject()
			if o.name:
				self.script_setWindow('')
			else:
				winUser.sendMessage(o.windowHandle,0x0002,0,0)
		nextHandler()

	def script_setWindow(self,gesture):
		fg=api.getForegroundObject()
		o=api.getFocusObject()
		if o.treeInterceptor:
			if fg.simpleFirstChild.name=='后退':
				ui.message(fg.simpleFirstChild.simpleNext.name)
			else:
				ui.message(fg.simpleFirstChild.name)
			return
		fgHandle=fg.windowHandle
		childHandle=winUser.user32.FindWindowExW(fgHandle,None,'XWeb_Chrome_WidgetWin_0',None)
		if childHandle:
			child=IAccessible.getNVDAObjectFromEvent(childHandle,0,0)
			api.setNavigatorObject(child.simpleFirstChild)
			api.setNavigatorObject(child.simpleFirstChild.simpleNext)
			o=api.getNavigatorObject()
			if o.role==52:
				eventHandler.executeEvent("gainFocus",o)

	def script_back(self,gesture):
		o=api.getForegroundObject().simpleFirstChild
		if 0X400 in o.states or 0x400000 in o.states:
			ui.message(u'无法返回')
			return
		if o.name=='后退':
			o.doAction()
		else:
			ui.message(o.name)

	def script_exit(self,gesture):
		o=api.getForegroundObject().simpleFirstChild
		while o.name!='关闭':
			o=o.simpleNext
		o.doAction()

	__gestures = {
'kb:f1':'setWindow',
"kb:f3" :"back",
"kb:f4" :"exit",
	}