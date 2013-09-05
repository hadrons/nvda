# -*- coding: UTF-8 -*-
#keyboardHandler.py
#A part of NonVisual Desktop Access (NVDA)
#This file is covered by the GNU General Public License.
#See the file COPYING for more details.
#Copyright (C) 2006-2010 Michael Curran <mick@kulgan.net>, James Teh <jamie@jantrid.net>, Peter Vágner <peter.v@datagate.sk>, Aleksey Sadovoy <lex@onm.su>

"""Keyboard support"""

import time
import re
import wx
import winUser
import vkCodes
import speech
import ui
from keyLabels import localizedKeyLabels
from logHandler import log
import queueHandler
import config
import api
import winInputHook
import inputCore
import tones

ignoreInjected=False

# Fake vk codes.
# These constants should be assigned to the name that NVDA will use for the key.
VK_WIN = "windows"

#: Keys which have been trapped by NVDA and should not be passed to the OS.
trappedKeys=set()
#: Tracks the number of keys passed through by request of the user.
#: If -1, pass through is disabled.
#: If 0 or higher then key downs and key ups will be passed straight through.
passKeyThroughCount=-1
#: The last key down passed through by request of the user.
lastPassThroughKeyDown = None
#: The last NVDA modifier key that was pressed with no subsequent key presses.
lastNVDAModifier = None
#: When the last NVDA modifier key was released.
lastNVDAModifierReleaseTime = None
#: Indicates that the NVDA modifier's special functionality should be bypassed until a key is next released.
bypassNVDAModifier = False
#: The modifiers currently being pressed.
currentModifiers = set()
#: A counter which is incremented each time a key is pressed.
#: Note that this may be removed in future, so reliance on it should generally be avoided.
#: @type: int
keyCounter = 0
#: The current sticky NVDa modifier key.
stickyNVDAModifier = None
#: Whether the sticky NVDA modifier is locked.
stickyNVDAModifierLocked = False

def passNextKeyThrough():
	global passKeyThroughCount
	if passKeyThroughCount==-1:
		passKeyThroughCount=0

def isNVDAModifierKey(vkCode,extended):
	if config.conf["keyboard"]["useNumpadInsertAsNVDAModifierKey"] and vkCode==winUser.VK_INSERT and not extended:
		return True
	elif config.conf["keyboard"]["useExtendedInsertAsNVDAModifierKey"] and vkCode==winUser.VK_INSERT and extended:
		return True
	elif config.conf["keyboard"]["useCapsLockAsNVDAModifierKey"] and vkCode==winUser.VK_CAPITAL:
		return True
	else:
		return False

def internal_keyDownEvent(vkCode,scanCode,extended,injected):
	"""Event called by winInputHook when it receives a keyDown.
	"""
	try:
		global lastNVDAModifier, lastNVDAModifierReleaseTime, bypassNVDAModifier, passKeyThroughCount, lastPassThroughKeyDown, currentModifiers, keyCounter, stickyNVDAModifier, stickyNVDAModifierLocked
		# Injected keys should be ignored in some cases.
		if ignoreInjected and injected:
			return True

		keyCode = (vkCode, extended)

		if passKeyThroughCount >= 0:
			# We're passing keys through.
			if lastPassThroughKeyDown != keyCode:
				# Increment the pass key through count.
				# We only do this if this isn't a repeat of the previous key down, as we don't receive key ups for repeated key downs.
				passKeyThroughCount += 1
				lastPassThroughKeyDown = keyCode
			return True

		keyCounter += 1
		stickyKeysFlags = winUser.getSystemStickyKeys().dwFlags
		if stickyNVDAModifier and not stickyKeysFlags & winUser.SKF_STICKYKEYSON:
			# Sticky keys has been disabled,
			# so clear the sticky NVDA modifier.
			currentModifiers.discard(stickyNVDAModifier)
			stickyNVDAModifier = None
			stickyNVDAModifierLocked = False
		gesture = KeyboardInputGesture(currentModifiers, vkCode, scanCode, extended)
		if not (stickyKeysFlags & winUser.SKF_STICKYKEYSON) and (bypassNVDAModifier or (keyCode == lastNVDAModifier and lastNVDAModifierReleaseTime and time.time() - lastNVDAModifierReleaseTime < 0.5)):
			# The user wants the key to serve its normal function instead of acting as an NVDA modifier key.
			# There may be key repeats, so ensure we do this until they stop.
			bypassNVDAModifier = True
			gesture.isNVDAModifierKey = False
		lastNVDAModifierReleaseTime = None
		if gesture.isNVDAModifierKey:
			lastNVDAModifier = keyCode
			if stickyKeysFlags & winUser.SKF_STICKYKEYSON:
				if keyCode == stickyNVDAModifier:
					if stickyKeysFlags & winUser.SKF_TRISTATE and not stickyNVDAModifierLocked:
						# The NVDA modifier is being locked.
						stickyNVDAModifierLocked = True
						if stickyKeysFlags & winUser.SKF_AUDIBLEFEEDBACK:
							tones.beep(1984, 60)
						return False
					else:
						# The NVDA modifier is being unlatched/unlocked.
						stickyNVDAModifier = None
						stickyNVDAModifierLocked = False
						if stickyKeysFlags & winUser.SKF_AUDIBLEFEEDBACK:
							tones.beep(496, 60)
						return False
				else:
					# The NVDA modifier is being latched.
					if stickyNVDAModifier:
						# Clear the previous sticky NVDA modifier.
						currentModifiers.discard(stickyNVDAModifier)
						stickyNVDAModifierLocked = False
					stickyNVDAModifier = keyCode
					if stickyKeysFlags & winUser.SKF_AUDIBLEFEEDBACK:
						tones.beep(1984, 60)
		else:
			# Another key was pressed after the last NVDA modifier key, so it should not be passed through on the next press.
			lastNVDAModifier = None
		if gesture.isModifier:
			if gesture.speechEffectWhenExecuted in (gesture.SPEECHEFFECT_PAUSE, gesture.SPEECHEFFECT_RESUME) and keyCode in currentModifiers:
				# Ignore key repeats for the pause speech key to avoid speech stuttering as it continually pauses and resumes.
				return True
			currentModifiers.add(keyCode)
		elif stickyNVDAModifier and not stickyNVDAModifierLocked:
			# A non-modifier was pressed, so unlatch the NVDA modifier.
			currentModifiers.discard(stickyNVDAModifier)
			stickyNVDAModifier = None

		try:
			inputCore.manager.executeGesture(gesture)
			trappedKeys.add(keyCode)
			return False
		except inputCore.NoInputGestureAction:
			if gesture.isNVDAModifierKey:
				# Never pass the NVDA modifier key to the OS.
				trappedKeys.add(keyCode)
				return False
	except:
		log.error("internal_keyDownEvent", exc_info=True)
	return True

def internal_keyUpEvent(vkCode,scanCode,extended,injected):
	"""Event called by winInputHook when it receives a keyUp.
	"""
	try:
		global lastNVDAModifier, lastNVDAModifierReleaseTime, bypassNVDAModifier, passKeyThroughCount, lastPassThroughKeyDown, currentModifiers
		if ignoreInjected and injected:
			return True

		keyCode = (vkCode, extended)

		if passKeyThroughCount >= 1:
			if lastPassThroughKeyDown == keyCode:
				# This key has been released.
				lastPassThroughKeyDown = None
			passKeyThroughCount -= 1
			if passKeyThroughCount == 0:
				passKeyThroughCount = -1
			return True

		if lastNVDAModifier and keyCode == lastNVDAModifier:
			# The last pressed NVDA modifier key is being released and there were no key presses in between.
			# The user may want to press it again quickly to pass it through.
			lastNVDAModifierReleaseTime = time.time()
		# If we were bypassing the NVDA modifier, stop doing so now, as there will be no more repeats.
		bypassNVDAModifier = False

		if keyCode != stickyNVDAModifier:
			currentModifiers.discard(keyCode)

		if keyCode in trappedKeys:
			trappedKeys.remove(keyCode)
			return False
	except:
		log.error("", exc_info=True)
	return True

#Register internal key press event with  operating system

def initialize():
	"""Initialises keyboard support."""
	winInputHook.initialize()
	winInputHook.setCallbacks(keyDown=internal_keyDownEvent,keyUp=internal_keyUpEvent)

def terminate():
	winInputHook.terminate()

def getInputHkl():
	"""Obtain the hkl currently being used for input.
	This retrieves the hkl from the thread of the focused window.
	"""
	focus = api.getFocusObject()
	if focus:
		thread = focus.windowThreadID
	else:
		thread = 0
	return winUser.user32.GetKeyboardLayout(thread)

class KeyboardInputGesture(inputCore.InputGesture):
	"""A key pressed on the traditional system keyboard.
	"""

	#: All normal modifier keys, where modifier vk codes are mapped to a more general modifier vk code or C{None} if not applicable.
	#: @type: dict
	NORMAL_MODIFIER_KEYS = {
		winUser.VK_LCONTROL: winUser.VK_CONTROL,
		winUser.VK_RCONTROL: winUser.VK_CONTROL,
		winUser.VK_CONTROL: None,
		winUser.VK_LSHIFT: winUser.VK_SHIFT,
		winUser.VK_RSHIFT: winUser.VK_SHIFT,
		winUser.VK_SHIFT: None,
		winUser.VK_LMENU: winUser.VK_MENU,
		winUser.VK_RMENU: winUser.VK_MENU,
		winUser.VK_MENU: None,
		winUser.VK_LWIN: VK_WIN,
		winUser.VK_RWIN: VK_WIN,
		VK_WIN: None,
	}

	#: All possible toggle key vk codes.
	#: @type: frozenset
	TOGGLE_KEYS = frozenset((winUser.VK_CAPITAL, winUser.VK_NUMLOCK, winUser.VK_SCROLL))

	#: All possible keyboard layouts, where layout names are mapped to localised layout names.
	#: @type: dict
	LAYOUTS = {
		# Translators: One of the keyboard layouts for NVDA.
		"desktop": _("desktop"),
		# Translators: One of the keyboard layouts for NVDA.
		"laptop": _("laptop"),
	}

	@classmethod
	def getVkName(cls, vkCode, isExtended):
		if isinstance(vkCode, str):
			return vkCode
		name = vkCodes.byCode.get((vkCode, isExtended))
		if not name and isExtended is not None:
			# Whether the key is extended doesn't matter for many keys, so try None.
			name = vkCodes.byCode.get((vkCode, None))
		return name if name else ""

	def __init__(self, modifiers, vkCode, scanCode, isExtended):
		#: The keyboard layout in which this gesture was created.
		#: @type: str
		self.layout = config.conf["keyboard"]["keyboardLayout"]
		self.modifiers = modifiers = set(modifiers)
		# Don't double up if this is a modifier key repeat.
		modifiers.discard((vkCode, isExtended))
		if vkCode in (winUser.VK_DIVIDE, winUser.VK_MULTIPLY, winUser.VK_SUBTRACT, winUser.VK_ADD) and winUser.getKeyState(winUser.VK_NUMLOCK) & 1:
			# Some numpad keys have the same vkCode regardless of numlock.
			# For these keys, treat numlock as a modifier.
			modifiers.add((winUser.VK_NUMLOCK, False))
		self.generalizedModifiers = set((self.NORMAL_MODIFIER_KEYS.get(mod) or mod, extended) for mod, extended in modifiers)
		self.vkCode = vkCode
		self.scanCode = scanCode
		self.isExtended = isExtended
		super(KeyboardInputGesture, self).__init__()

	def _get_isNVDAModifierKey(self):
		return isNVDAModifierKey(self.vkCode, self.isExtended)

	def _get_isModifier(self):
		return self.vkCode in self.NORMAL_MODIFIER_KEYS or self.isNVDAModifierKey

	def _get_mainKeyName(self):
		if self.isNVDAModifierKey:
			return "NVDA"

		name = self.getVkName(self.vkCode, self.isExtended)
		if name:
			return name

		if 32 < self.vkCode < 128:
			return unichr(self.vkCode).lower()
		vkChar = winUser.user32.MapVirtualKeyExW(self.vkCode, winUser.MAPVK_VK_TO_CHAR, getInputHkl())
		if vkChar>0:
			if vkChar == 43: # "+"
				# A gesture identifier can't include "+" except as a separator.
				return "plus"
			return unichr(vkChar).lower()

		return winUser.getKeyNameText(self.scanCode, self.isExtended)

	def _get_modifierNames(self):
		modTexts = set()
		for modVk, modExt in self.generalizedModifiers:
			if isNVDAModifierKey(modVk, modExt):
				modTexts.add("NVDA")
			else:
				modTexts.add(self.getVkName(modVk, None))

		return modTexts

	def _get__keyNamesInDisplayOrder(self):
		return tuple(self.modifierNames) + (self.mainKeyName,)

	def _get_logIdentifier(self):
		return u"kb({layout}):{key}".format(layout=self.layout,
			key="+".join(self._keyNamesInDisplayOrder))

	def _get_displayName(self):
		return "+".join(localizedKeyLabels.get(key.lower(), key) for key in self._keyNamesInDisplayOrder)

	def _get_identifiers(self):
		keyNames = set(self.modifierNames)
		keyNames.add(self.mainKeyName)
		keyName = "+".join(keyNames).lower()
		return (
			u"kb({layout}):{key}".format(layout=self.layout, key=keyName),
			u"kb:{key}".format(key=keyName)
		)

	def _get_shouldReportAsCommand(self):
		if self.isExtended and winUser.VK_VOLUME_MUTE <= self.vkCode <= winUser.VK_VOLUME_UP:
			# Don't report volume controlling keys.
			return False
		# Aside from space, a key name of more than 1 character is a command.
		if self.vkCode != winUser.VK_SPACE and len(self.mainKeyName) > 1:
			return True
		# If this key has modifiers other than shift, it is a command; e.g. shift+f is text, but control+f is a command.
		modifiers = self.generalizedModifiers
		if modifiers and (len(modifiers) > 1 or tuple(modifiers)[0][0] != winUser.VK_SHIFT):
			return True
		return False

	def _get_speechEffectWhenExecuted(self):
		if inputCore.manager.isInputHelpActive:
			return self.SPEECHEFFECT_CANCEL
		if self.isExtended and winUser.VK_VOLUME_MUTE <= self.vkCode <= winUser.VK_VOLUME_UP:
			return None
		if not config.conf['keyboard']['speechInterruptForCharacters'] and (not self.shouldReportAsCommand or self.vkCode in (winUser.VK_SHIFT, winUser.VK_LSHIFT, winUser.VK_RSHIFT)):
			return None
		if self.vkCode==winUser.VK_RETURN and not config.conf['keyboard']['speechInterruptForEnter']:
			return None
		if self.vkCode in (winUser.VK_SHIFT, winUser.VK_LSHIFT, winUser.VK_RSHIFT):
			return self.SPEECHEFFECT_RESUME if speech.isPaused else self.SPEECHEFFECT_PAUSE
		return self.SPEECHEFFECT_CANCEL

	def reportExtra(self):
		if self.vkCode in self.TOGGLE_KEYS:
			wx.CallLater(30, self._reportToggleKey)

	def _reportToggleKey(self):
		toggleState = winUser.getKeyState(self.vkCode) & 1
		key = self.mainKeyName
		ui.message(u"{key} {state}".format(
			key=localizedKeyLabels.get(key.lower(), key),
			state=_("on") if toggleState else _("off")))

	def send(self):
		global ignoreInjected
		keys = []
		for vk, ext in self.generalizedModifiers:
			if vk == VK_WIN:
				if winUser.getKeyState(winUser.VK_LWIN) & 32768 or winUser.getKeyState(winUser.VK_RWIN) & 32768:
					# Already down.
					continue
				vk = winUser.VK_LWIN
			elif winUser.getKeyState(vk) & 32768:
				# Already down.
				continue
			keys.append((vk, 0, ext))
		keys.append((self.vkCode, self.scanCode, self.isExtended))

		try:
			ignoreInjected=True
			if winUser.getKeyState(self.vkCode) & 32768:
				# This key is already down, so send a key up for it first.
				winUser.keybd_event(self.vkCode, self.scanCode, self.isExtended + 2, 0)

			# Send key down events for these keys.
			for vk, scan, ext in keys:
				winUser.keybd_event(vk, scan, ext, 0)
			# Send key up events for the keys in reverse order.
			for vk, scan, ext in reversed(keys):
				winUser.keybd_event(vk, scan, ext + 2, 0)

			if not queueHandler.isPendingItems(queueHandler.eventQueue):
				time.sleep(0.01)
				wx.Yield()
		finally:
			ignoreInjected=False

	@classmethod
	def fromName(cls, name):
		"""Create an instance given a key name.
		@param name: The key name.
		@type name: str
		@return: A gesture for the specified key.
		@rtype: L{KeyboardInputGesture}
		"""
		keyNames = name.split("+")
		keys = []
		for keyName in keyNames:
			if keyName == "plus":
				# A key name can't include "+" except as a separator.
				keyName = "+"
			if keyName == VK_WIN:
				vk = winUser.VK_LWIN
				ext = False
			elif len(keyName) == 1:
				ext = False
				requiredMods, vk = winUser.VkKeyScanEx(keyName, getInputHkl())
				if requiredMods & 1:
					keys.append((winUser.VK_SHIFT, False))
				if requiredMods & 2:
					keys.append((winUser.VK_CONTROL, False))
				if requiredMods & 4:
					keys.append((winUser.VK_MENU, False))
				# Not sure whether we need to support the Hankaku modifier (& 8).
			else:
				vk, ext = vkCodes.byName[keyName.lower()]
				if ext is None:
					ext = False
			keys.append((vk, ext))

		if not keys:
			raise ValueError

		return cls(keys[:-1], vk, 0, ext)

	RE_IDENTIFIER = re.compile(r"^kb(?:\((.+?)\))?:(.*)$")
	@classmethod
	def getDisplayTextForIdentifier(cls, identifier):
		layout, keys = cls.RE_IDENTIFIER.match(identifier).groups()
		dispSource = None
		if layout:
			try:
				# Translators: Used when describing keys on the system keyboard with a particular layout.
				# %s is replaced with the layout name.
				# For example, in English, this might produce "laptop keyboard".
				dispSource = _("%s keyboard") % cls.LAYOUTS[layout]
			except KeyError:
				pass
		if not dispSource:
			# Translators: Used when describing keys on the system keyboard applying to all layouts.
			dispSource = _("keyboard, all layouts")

		keys = set(keys.split("+"))
		names = []
		main = None
		try:
			# If present, the NVDA key should appear first.
			keys.remove("nvda")
			names.append("NVDA")
		except KeyError:
			pass
		for key in keys:
			label = localizedKeyLabels.get(key, key)
			vk = vkCodes.byName.get(key)
			# vkCodes.byName values are (vk, ext)
			if vk:
				vk = vk[0]
			if vk in cls.NORMAL_MODIFIER_KEYS:
				names.append(label)
			else:
				# The main key must be last, so handle that outside the loop.
				main = label
		names.append(main)
		return dispSource, "+".join(names)

inputCore.registerGestureSource("kb", KeyboardInputGesture)
