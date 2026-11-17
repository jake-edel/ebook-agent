"""`gg` and `G`: to the top and the bottom, as in Vim. Shared by the panes."""

from textual.binding import Binding

JUMP_KEYS = [
    Binding("g", "jump_top", "Top", show=False),  # gg: see JumpKeys
    Binding("G", "jump_bottom", "Bottom", show=False),
]


class JumpKeys:
    """Mixed into a widget, before the widget class, along with JUMP_KEYS in
    its BINDINGS. The widget says where the top and bottom are, with
    jump_to_top() and jump_to_bottom()."""

    _g_pressed = False

    def on_key(self, event):
        # `gg` is two presses of g; anything else in between cancels it
        if event.key != "g":
            self._g_pressed = False

    def action_jump_top(self):
        if self._g_pressed:
            self.jump_to_top()
        self._g_pressed = not self._g_pressed

    def action_jump_bottom(self):
        self._g_pressed = False
        self.jump_to_bottom()

    def jump_to_top(self):
        raise NotImplementedError

    def jump_to_bottom(self):
        raise NotImplementedError
