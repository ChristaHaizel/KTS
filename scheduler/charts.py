"""Server-rendered SVG geometry for the convergence chart.

Computing the points here keeps the template free of arithmetic and means the
chart needs no JavaScript and no charting library - it is plain SVG that works
offline and prints.
"""

# Plot box. The bottom padding is deliberately large enough to hold the x-axis
# labels inside the viewBox, so the chart never needs its own scrollbar.
WIDTH = 720
HEIGHT = 320
PAD_LEFT = 52
PAD_RIGHT = 24
PAD_TOP = 20
PAD_BOTTOM = 44

PLOT_WIDTH = WIDTH - PAD_LEFT - PAD_RIGHT
PLOT_HEIGHT = HEIGHT - PAD_TOP - PAD_BOTTOM

Y_TICKS = [0.0, 0.25, 0.5, 0.75, 1.0]


def _x(index, count):
    if count <= 1:
        return PAD_LEFT
    return PAD_LEFT + (index / (count - 1)) * PLOT_WIDTH


def _y(value):
    return PAD_TOP + (1 - value) * PLOT_HEIGHT


def convergence_chart(history):
    """Geometry for a best-fitness-per-generation line.

    One series, so no legend: the heading names it. The final point is labelled
    directly rather than every point carrying a number.
    """
    if not history:
        return None

    count = len(history)
    points = [(_x(i, count), _y(v)) for i, v in enumerate(history)]

    # A handful of x labels regardless of how many generations ran.
    if count == 1:
        tick_indexes = [0]
    else:
        step = max(1, (count - 1) // 4)
        tick_indexes = list(range(0, count, step))
        if tick_indexes[-1] != count - 1:
            tick_indexes.append(count - 1)

    return {
        'width': WIDTH,
        'height': HEIGHT,
        'pad_left': PAD_LEFT,
        'pad_top': PAD_TOP,
        'plot_width': PLOT_WIDTH,
        'plot_bottom': PAD_TOP + PLOT_HEIGHT,
        'polyline': ' '.join(f'{x:.1f},{y:.1f}' for x, y in points),
        'single_point': points[0] if count == 1 else None,
        'last_point': {
            'x': points[-1][0],
            'y': points[-1][1],
            'label': f'{history[-1]:.4f}',
            # Keep the end label inside the plot when the line finishes at the right.
            'anchor': 'end' if points[-1][0] > PAD_LEFT + PLOT_WIDTH * 0.85 else 'start',
            'dx': -10 if points[-1][0] > PAD_LEFT + PLOT_WIDTH * 0.85 else 10,
        },
        'y_ticks': [
            {'value': f'{t:.2f}', 'y': _y(t)} for t in Y_TICKS
        ],
        'x_ticks': [
            {'label': str(i + 1), 'x': _x(i, count)} for i in tick_indexes
        ],
    }
