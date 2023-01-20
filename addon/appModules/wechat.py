import wx
import appModuleHandler
import speech
import config
import ui
import api
import eventHandler
import controlTypes
import mouseHandler
import winUser
from NVDAObjects import NVDAObjectTextInfo
from versionInfo import version_year
from scriptHandler import script
from nvwave import playWaveFile
from os.path import join, dirname
role = controlTypes.Role if version_year>=2022 else controlTypes.role.Role

class AppModule(appModuleHandler.AppModule):
	ReportOCRResultTimer=None
	SaySessionTitleTimer = None
	sectionTitleObject=None
	oldSectionTitle=None
	OCRResult=None
	SOUND_LINK = join(dirname(__file__), "link.wav")
	SOUND_UNREAD = join(dirname(__file__), "unread.wav")

	SOUND_POPUP = join(dirname(__file__), "popup.wav")
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
		try:
			if obj.role==role.LISTITEM and obj.parent.name=="消息" and obj.simpleFirstChild:
				playWaveFile(self.SOUND_POPUP)
				if self.isAutoMSG:
					if obj.name==None:
						children = obj.recursiveDescendants
						for child in children:
							if not speech.isBlank(child.name): ui.message(child.name)
					elif obj.simpleFirstChild.role==role.BUTTON:
						ui.message("%s 说： %s" % (obj.simpleFirstChild.name,obj.name))
						if obj.value: ui.message(obj.value)
					elif obj.simpleFirstChild.role==role.EDITABLETEXT:
						ui.message("%s 说： %s" % (obj.simpleLastChild.name,obj.name))
						if obj.value: ui.message(obj.value)
		except: pass
		nextHandler()

	def event_gainFocus(self, obj, nextHandler, isFocus=False):
		if (obj.name=="输入") and obj.windowClassName=="WeChatMainWndForPC":
			if self.SaySessionTitleTimer:
				self.SaySessionTitleTimer.Stop()
			self.SaySessionTitle()
		else:
			if self.SaySessionTitleTimer:
				self.SaySessionTitleTimer.Stop()

		try:
			if obj.simpleParent.name == "会话" and obj.simpleParent.role == role.LIST:
				if role.STATICTEXT == obj.simpleLastChild.next.role:
					if obj.simpleLastChild.name.isdigit():
						playWaveFile(self.SOUND_UNREAD)
						obj.name+=obj.simpleLastChild.name + "条未读"
		except: pass

		try:
			if obj.role==role.LISTITEM and obj.parent.name=="消息":
				if obj.value !=None:
					playWaveFile(self.SOUND_LINK)
		except: pass
		try:
			if obj.role==role.BUTTON and obj.simpleParent.role==role.LISTITEM:
				if obj.next.firstChild.firstChild.role==role.STATICTEXT:
					obj.name=obj.next.firstChild.firstChild.name
		except: pass

		if obj.role==role.BUTTON and obj.name=="sendBtn":
			obj.name="发送(S)"

		try:
			if obj.name==None:
				if obj.role==role.CHECKBOX:
					ui.message(obj.simpleFirstChild.simpleNext.simpleNext.name)
				if obj.role==role.LISTITEM:
					children = obj.recursiveDescendants
					for child in children:
						if child.role==role.CHECKBOX: speech.speakObject(child)
						elif not speech.isBlank(child.name): ui.message(child.name)
				if obj.role==role.PANE and obj.windowClassName=="WeChatMainWndForPC":
					return
			elif obj.treeInterceptor and obj.role==role.LIST and "discuss_list" == obj.IA2Attributes.get("class"):
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
		description="是否自动朗读新消息",
		category="PC微信增强",
		gesture="kb:f3"
	)
	def script_autoMSG(self,gesture):
		self.isAutoMSG=not self.isAutoMSG
		config.conf["WeChatEnhancement"]["isAutoMSG"]=self.isAutoMSG
		if self.isAutoMSG:
			ui.message("自动读出新消息")
		else:
			ui.message("默认")

	def event_foreground(self, obj, nextHandler):
		if obj.windowClassName == "ImagePreviewWnd":
			wx.CallLater(800, self.clickButton, "提取文字")
			wx.CallLater(100, self.ReportOCRResult)
		elif obj.windowClassName in("CefWebViewWnd", "SubscriptionWnd"):
			wx.CallLater(100, self.FindDocumentObject)
		else:
			if self.SaySessionTitleTimer:
				self.SaySessionTitleTimer.Stop()
			if self.ReportOCRResultTimer:
				self.ReportOCRResultTimer.Stop()
				self.OCRResult=None
		nextHandler()

	def ReportOCRResult(self):
		fg=api.getForegroundObject()
		try:
			if fg.simpleLastChild.role==role.LIST:
				if self.OCRResult != fg.simpleLastChild.simpleFirstChild.name:
					self.OCRResult = fg.simpleLastChild.simpleFirstChild.name
					ui.message(self.OCRResult)
					fg.simpleLastChild.name = self.OCRResult
			else: self.OCRResult = None
		except: pass
		self.ReportOCRResultTimer = wx.CallLater(100, self.ReportOCRResult)

	@script(
		description="定位网页文档控件",
		category="PC微信增强",
		gesture="kb:f6"
	)
	def script_setWindow(self,gesture):
		self.FindDocumentObject()


	def FindDocumentObject(self):
		fg=api.getForegroundObject()
		if fg.simpleFirstChild:
			child=fg.simpleFirstChild
		elif child.name=="后退":
			child = child.simpleParent.simpleFirstChild.simpleNext
		api.setNavigatorObject(child)
		obj=api.getNavigatorObject()
		if obj.role==role.DOCUMENT:
			eventHandler.executeEvent("gainFocus",obj)

	def SaySessionTitle(self):
		FocusObj=api.getFocusObject()
#		if not (FocusObj.name=="输入" and FocusObj.windowClassName=="WeChatMainWndForPC"):
#			import tones
#			tones.beep(100,100)
#			return
		if not self.sectionTitleObject:
			self.sectionTitleObject=self.getSectionTitleObject()
			if not self.sectionTitleObject or self.sectionTitleObject.name in ("置顶", "订阅号", "列表模式", "卡片模式"):
#			if not self.sectionTitleObject:
#				self.sectionTitleObject=None
				return
		title=self.sectionTitleObject.name
		if (title != self.oldSectionTitle):
			self.oldSectionTitle=title
			speech.cancelSpeech()
			ui.message(title)
		self.SaySessionTitleTimer = wx.CallLater(50, self.SaySessionTitle)

	def getSectionTitleObject(self):
		obj=api.getForegroundObject()
		try:
			obj=obj.simpleFirstChild
			while obj.name!="联系人":
				obj=obj.simpleNext
			if obj.simpleNext:
				return obj.simpleNext
		except:
			pass

	@script(
		description="微信内置浏览器后退到上一页",
		category="PC微信增强",
		gesture="kb:alt+leftArrow"
	)
	def script_back(self,gesture):
		if api.getForegroundObject().windowClassName == "CefWebViewWnd":
			self.clickButton("后退")
			wx.CallLater(100, self.FindDocumentObject)

	@script(
		description="关闭微信内置浏览器窗口",
		category="PC微信增强",
		gesture="kb:control+w"
	)
	def script_close(self,gesture):
		if api.getForegroundObject().windowClassName in ("CefWebViewWnd", "ImagePreviewWnd"):
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
