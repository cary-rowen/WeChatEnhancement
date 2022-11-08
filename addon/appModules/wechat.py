
import appModuleHandler
import speech
import config
import ui
import api
import eventHandler
import winUser
import controlTypes
from NVDAObjects import NVDAObjectTextInfo
from versionInfo import version_year
from scriptHandler import script
from nvwave import playWaveFile
from os.path import join, dirname
role = controlTypes.Role if version_year>=2022 else controlTypes.role.Role

class AppModule(appModuleHandler.AppModule):
	SOUND_LINK = join(dirname(__file__), 'link.wav')
	SOUND_POPUP = join(dirname(__file__), 'popup.wav')
	confspec = {
	"isAutoMSG": "boolean(default=False)"
	}
	config.conf.spec["WeChatEnhancement"] = confspec
	isAutoMSG = config.conf["WeChatEnhancement"]["isAutoMSG"]

	def event_NVDAObject_init(self, obj):
		if role.EDITABLETEXT != obj.role:
			obj.displayText = obj.name
			obj.TextInfo = NVDAObjectTextInfo

	def event_nameChange(self, obj, nextHandler):
		if obj.role==role.LISTITEM and obj.parent.name=='消息' and obj.simpleFirstChild:
			playWaveFile(self.SOUND_POPUP)
			if self.isAutoMSG:
				if obj.name==None:
					children = obj.recursiveDescendants
					for child in children:
						if not speech.isBlank(child.name): ui.message(child.name)
				elif obj.simpleFirstChild.role==role.BUTTON:
					ui.message('%s 说： %s' % (obj.simpleFirstChild.name,obj.name))
					if obj.value: ui.message(obj.value)
				elif obj.simpleFirstChild.role==role.EDITABLETEXT:
					ui.message('%s 说： %s' % (obj.simpleLastChild.name,obj.name))
					if obj.value: ui.message(obj.value)
		nextHandler()

	def event_gainFocus(self, obj, nextHandler, isFocus=False):
		if obj.role==role.LISTITEM and obj.parent.name=='消息':
			if obj.value !=None: 			playWaveFile(self.SOUND_LINK)
		if obj.role==role.BUTTON and obj.simpleParent.role==role.LISTITEM:
			try:
				if obj.next.firstChild.firstChild.role==role.STATICTEXT:
					obj.name=obj.next.firstChild.firstChild.name
			except: pass
		if obj.role==role.BUTTON and obj.name=='sendBtn':
			obj.name='发送(S)'
		if 'NetErrInfoTipsBarWnd' == obj.windowClassName:
			ui.message (obj.displayText)
			return
		if obj.name==None:
			if obj.role==role.CHECKBOX:
				ui.message(obj.simpleFirstChild.name)
			if obj.role==role.LISTITEM:
				children = obj.recursiveDescendants
				for child in children:
					if child.role==role.CHECKBOX: speech.speakObject(child)
					elif not speech.isBlank(child.name): ui.message(child.name)
		elif obj.treeInterceptor and obj.role==role.LIST and 'discuss_list' == obj.IA2Attributes.get('class'):
			o=obj.firstChild.firstChild
			while o:
				ui.message(o.firstChild.name)
				o=o.next
		elif obj.treeInterceptor and obj.role==role.LISTITEM and 'js_comment' in obj.IA2Attributes.get('class'):
			ui.message(obj.firstChild.name)
		elif obj.treeInterceptor and obj.role==role.BUTTON and 'sns_opr_btn sns_praise_btn' == obj.IA2Attributes.get('class'):
			ui.message(obj.simplePrevious.name)
		nextHandler()


	@script(
		description='是否自动朗读新消息',
		category='PC微信增强',
		gesture='kb:f3'
	)
	def script_autoMSG(self,gesture):
		self.isAutoMSG=not self.isAutoMSG
		config.conf["WeChatEnhancement"]["isAutoMSG"]=self.isAutoMSG
		if self.isAutoMSG:
			ui.message("自动读出新消息")
		else:
			ui.message("默认")

	def event_foreground(self, obj, nextHandler):
		if obj.windowClassName in('CefWebViewWnd', 'SubscriptionWnd'):
			from wx import CallLater
			CallLater(80, self.FindDocumentObject)
		nextHandler()

	@script(
		description='寻找网页文档控件',
		category='PC微信增强',
		gesture='kb:f1'
	)
	def script_setWindow(self,gesture):
		self.FindDocumentObject()

	def FindDocumentObject(self):
		fg=api.getForegroundObject()
		if fg.simpleFirstChild:
			child=fg.simpleFirstChild
		elif child.name=='后退':
			from tones import beep
			beep(100,100)
			child = child.simpleParent.simpleFirstChild.simpleNext
		api.setNavigatorObject(child)
		obj=api.getNavigatorObject()
		if obj.role==role.DOCUMENT:
			from tones import beep
			beep(500,100)
			eventHandler.executeEvent("gainFocus",obj)

