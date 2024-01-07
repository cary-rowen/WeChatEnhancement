import appModuleHandler
import controlTypes
import ui
from versionInfo import version_year

role = controlTypes.Role if version_year >= 2022 else controlTypes.role.Role


def find_nth_from_end(string, substring, n):
	if n <= 0 or substring not in string:
		return -1
	count = string.count(substring)
	if n > count:  # 如果n大于出现次数，则直接返回-1
		return -1
	start = len(string)
	for _ in range(n):
		idx = string.rfind(substring, 0, start)
		start = idx  # 更新下次查找的结束位置为当前找到的位置
	return start  # 返回找到的索引


class AppModule(appModuleHandler.AppModule):

	def event_gainFocus(self, obj, nextHandler, isFocus=False):
		message = []
		if obj.treeInterceptor and obj.role == role.BUTTON \
		and obj.IA2Attributes.get('class', '') == 'sns_opr_btn sns_praise_btn':
			while "discuss_user_avatar" not in obj.IA2Attributes.get('class', ''):
				if obj.IA2Attributes.get('display', '') == 'inline':
					message.append(obj.name)
				obj = obj.simplePrevious
			temp = obj.name
			parts = temp.split(' ，')
			temp = '，'.join(parts[:-3]) if len(parts) >= 3 else temp
			message.append(temp)
			ui.message('，'.join(message))
		nextHandler()
