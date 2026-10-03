"""Screen-independent placement for the transcription overlay."""


def clamp(value, low, high):
    return max(low, min(high, value))


def overlay_position(area, size, position="top", anchor=None, margin=28):
    """Return an overlay origin within a monitor workarea.

    A custom anchor describes the available travel distance, not screen pixels.
    This keeps a dragged caption in the same relative place after resolution changes.
    """
    x, y, width, height = area
    box_width, box_height = size
    travel_x = max(0, width - box_width)
    travel_y = max(0, height - box_height)
    gap_x = min(margin, travel_x)
    gap_y = min(margin, travel_y)
    if position == "custom" and anchor is not None:
        offset_x = round(clamp(anchor[0], 0, 1) * travel_x)
        offset_y = round(clamp(anchor[1], 0, 1) * travel_y)
    elif position == "bottom":
        offset_x, offset_y = travel_x // 2, travel_y - gap_y
    elif position == "left":
        offset_x, offset_y = gap_x, travel_y // 2
    elif position == "right":
        offset_x, offset_y = travel_x - gap_x, travel_y // 2
    else:
        offset_x, offset_y = travel_x // 2, gap_y
    return x + offset_x, y + offset_y


def anchor_from_position(area, size, origin):
    x, y, width, height = area
    box_width, box_height = size
    travel_x = max(0, width - box_width)
    travel_y = max(0, height - box_height)
    return [round(clamp((origin[0] - x) / travel_x, 0, 1), 4) if travel_x else 0,
            round(clamp((origin[1] - y) / travel_y, 0, 1), 4) if travel_y else 0]


def monitor_for_window(monitors, window):
    """Choose monitor containing the target center, or nearest to it."""
    if not monitors:
        raise ValueError("没有可用显示器")
    if window is None:
        return monitors[0]
    x, y, width, height = window
    cx, cy = x + width / 2, y + height / 2
    def distance(area):
        ax, ay, aw, ah = area
        dx = max(ax - cx, 0, cx - (ax + aw))
        dy = max(ay - cy, 0, cy - (ay + ah))
        return dx * dx + dy * dy
    return min(monitors, key=distance)
