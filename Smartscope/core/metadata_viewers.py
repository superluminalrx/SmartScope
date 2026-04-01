"""Metadata viewers that read values from the Selector table.

Each viewer maps a Selector method_name to a continuous colormap,
displaying real values in the legend (like CTFFitViewer).
"""

from matplotlib import cm
from matplotlib.colors import rgb2hex
from math import floor
from typing import List, Optional
from Smartscope.lib.Datatypes.base_plugin import BaseFeatureAnalyzer


class SelectorMetadataViewer(BaseFeatureAnalyzer):
    """Generic metadata viewer backed by Selector rows."""

    name: str = ''
    description: str = ''
    selector_method: str = ''  # Selector.method_name to read from
    range: List[float] = [0.0, 100.0]
    step: float = 1.0
    unit: str = ''
    invert: bool = False  # True = lower is better (green→red)
    colors: List = []

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.colors = self._build_colors()

    def _build_colors(self):
        colors = []
        steps = range(
            int(self.range[0] / self.step),
            int(self.range[1] / self.step) + 1,
        )
        cmap = cm.plasma
        n_steps = len(steps)
        if n_steps == 0:
            return [('#808080', 0, '')]
        cmap_step = max(1, int(floor(cmap.N / n_steps)))
        color_range = range(cmap.N, 0, -cmap_step) if not self.invert else range(0, cmap.N, cmap_step)
        for c, v in zip(color_range, steps):
            prefix = ''
            val = v * self.step
            if val == self.range[0]:
                prefix = '\u2264'
            if val == self.range[1]:
                prefix = '\u2265'
            label = f'{val}{self.unit}' if self.unit else val
            colors.append((rgb2hex(cmap(c)), label, prefix))
        return colors

    def get_label(self, value):
        if value is None:
            return '#808080', 'N.D.', ''
        if value > self.range[1]:
            value = self.range[1]
        if value < self.range[0]:
            value = self.range[0]
        idx = floor((value - self.range[0]) / self.step)
        idx = max(0, min(idx, len(self.colors) - 1))
        return self.colors[idx]


# ---- Concrete viewers for preprocessing results ----

PREPROCESSING_METADATA_VIEWERS = [
    SelectorMetadataViewer(
        name='Particle count',
        description='Number of particles picked per micrograph',
        selector_method='Particle count',
        range=[0, 50],
        step=5,
    ),
    SelectorMetadataViewer(
        name='Pick score',
        description='Mean picking confidence score',
        selector_method='Pick score',
        range=[0.0, 1.0],
        step=0.1,
    ),
    SelectorMetadataViewer(
        name='CTF resolution',
        description='CTF fit resolution (\u00c5) — lower is better',
        selector_method='CTF resolution',
        range=[3, 10],
        step=0.5,
        unit='\u00c5',
        invert=True,
    ),
    SelectorMetadataViewer(
        name='Total motion',
        description='Total beam-induced motion (\u00c5) — lower is better',
        selector_method='Total motion',
        range=[0, 30],
        step=2,
        unit='\u00c5',
        invert=True,
    ),
    SelectorMetadataViewer(
        name='Ice thickness',
        description='Estimated ice thickness (nm)',
        selector_method='Ice thickness',
        range=[0, 100],
        step=10,
        unit='nm',
    ),
]
