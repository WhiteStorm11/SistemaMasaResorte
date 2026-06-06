"""
Simulador de Sistema Masa-Resorte-Amortiguador con Control Difuso Mamdani
=========================================================================
Implementación completa sin librerías difusas externas.
Interfaz gráfica interactiva con matplotlib.
"""

import numpy as np
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.animation as animation
from matplotlib.widgets import Slider, Button
from matplotlib.gridspec import GridSpec
from collections import deque


# ──────────────────────────────────────────────────────────────
#  CONTROLADOR DIFUSO MAMDANI
# ──────────────────────────────────────────────────────────────

class FuzzyMRAController:
    """
    Controlador difuso Mamdani para el sistema masa-resorte-amortiguador.

    Entradas:
        e  – error de posición  (universo [-4, 4] m)
        de – derivada del error (universo [-6, 6] m/s)
    Salida:
        F  – fuerza de control  (universo [-40, 40] N)

    Conjuntos lingüísticos: NB, N, Z, P, PB (triangulares simétricos)
    Defuzzificación: centroide. Todo el cómputo es vectorizado con numpy.
    """

    # Base de reglas 5×5: fila=índice de e, columna=índice de de
    # 0=NB, 1=N, 2=Z, 3=P, 4=PB
    _RULE_ARRAY = np.array([
        [4, 4, 3, 3, 2],   # e=NB → [PB, PB, P,  P,  Z ]
        [4, 3, 3, 2, 1],   # e=N  → [PB, P,  P,  Z,  N ]
        [3, 3, 2, 1, 1],   # e=Z  → [P,  P,  Z,  N,  N ]
        [3, 2, 1, 1, 0],   # e=P  → [P,  Z,  N,  N,  NB]
        [2, 1, 1, 0, 0],   # e=PB → [Z,  N,  N,  NB, NB]
    ])

    # Resolución del universo de salida para defuzzificación
    _N_UNIVERSE = 201

    def __init__(self):
        # Centros de los conjuntos triangulares
        self._ce  = np.linspace(-4,  4, 5)   # error
        self._cde = np.linspace(-6,  6, 5)   # derivada del error
        # Orden inverso: índice 0 (NB) → +40 N, índice 4 (PB) → -40 N
        # Necesario para que la tabla de reglas sea coherente con la planta
        self._cF  = np.linspace(40, -40, 5)  # fuerza

        # Semianchos de los triángulos
        self._hwe  = 2.0
        self._hwde = 3.0
        self._hwF  = 20.0

        # Universo de salida (vector fijo)
        self._Fu = np.linspace(-40.0, 40.0, self._N_UNIVERSE)

        # Precomputar las 5 funciones de membresía de salida sobre _Fu
        # Forma: (5, N_UNIVERSE) — se reutilizan en cada llamada a infer()
        self._muF_pre = np.maximum(
            0.0,
            1.0 - np.abs(self._Fu[None, :] - self._cF[:, None]) / self._hwF
        )  # (5, N_UNIVERSE)

        # Máscara booleana: para cada conjunto de salida k, qué reglas producen k
        # Forma: (5, 5, 5) → [k, i, j] = True si RULE[i,j]==k
        self._rule_mask = np.stack(
            [self._RULE_ARRAY == k for k in range(5)]
        )  # (5, 5, 5)

    # ── Fuzzificación vectorizada ─────────────────────────────
    def _fuzzify_vec(self, val: float, centers: np.ndarray,
                     hw: float) -> np.ndarray:
        """Calcula los 5 grados de membresía triangular con numpy puro."""
        return np.maximum(0.0, 1.0 - np.abs(val - centers) / hw)

    # ── Inferencia y defuzzificación ─────────────────────────
    def infer(self, e: float, de: float) -> float:
        """
        Ciclo completo de inferencia Mamdani completamente vectorizado.

        1. Fuzzifica e y de (vectorizado).
        2. Calcula las 25 activaciones con producto exterior + mínimo.
        3. Para cada conjunto de salida toma el máximo alpha entre sus reglas.
        4. Construye la función agregada por mínimo(alpha, muF_pre[k]).
        5. Defuzzifica por centroide.

        Parámetros
        ----------
        e  : error de posición (m)
        de : derivada del error (m/s)

        Retorna
        -------
        F  : fuerza de control (N) acotada a [-40, 40]
        """
        # Fuzzificación — resultado: (5,) cada uno
        mu_e  = self._fuzzify_vec(np.clip(e,  -4.,  4.),  self._ce,  self._hwe)
        mu_de = self._fuzzify_vec(np.clip(de, -6.,  6.),  self._cde, self._hwde)

        # Activaciones de las 25 reglas: alpha[i,j] = min(mu_e[i], mu_de[j])
        # Producto exterior con mínimo → (5, 5)
        alpha = np.minimum(mu_e[:, None], mu_de[None, :])

        # Para cada conjunto de salida k: alpha máximo entre todas sus reglas
        # _rule_mask[k]: (5,5) booleana → aplica máscara y reduce
        # Resultado: vector (5,) con el "corte" de cada conjunto de salida
        alpha_k = np.array([
            alpha[self._rule_mask[k]].max() if self._rule_mask[k].any() else 0.0
            for k in range(5)
        ])  # (5,)

        # Agregación: para cada k, recortar muF_pre[k] a alpha_k[k]
        # Luego tomar el máximo puntual → función agregada (N_UNIVERSE,)
        # np.minimum broadcast: (5,1) y (5, N_UNIVERSE) → (5, N_UNIVERSE)
        clipped = np.minimum(alpha_k[:, None], self._muF_pre)  # (5, N_UNIVERSE)
        agg     = clipped.max(axis=0)                          # (N_UNIVERSE,)

        # Defuzzificación por centroide
        denom = agg.sum()
        if denom < 1e-9:
            return 0.0

        return float(np.clip((self._Fu * agg).sum() / denom, -40., 40.))


# ──────────────────────────────────────────────────────────────
#  PLANTA: SISTEMA MASA-RESORTE-AMORTIGUADOR
# ──────────────────────────────────────────────────────────────

class MRASystem:
    """
    Modelo de la planta masa-resorte-amortiguador.

    Ecuación de movimiento:  m·x'' + b·x' + k·x = F(t)
    Integración: Euler explícito, dt = 0.02 s.
    """

    def __init__(self, m: float = 1.0, k: float = 10.0,
                 b: float = 1.0, dt: float = 0.02):
        self.m  = m
        self.k  = k
        self.b  = b
        self.dt = dt
        self.x  = 0.0
        self.v  = 0.0

    def step(self, F: float):
        """Avanza un paso Euler: devuelve (x, v) actualizados."""
        a      = (F - self.b * self.v - self.k * self.x) / self.m
        self.x += self.v * self.dt
        self.v += a      * self.dt
        return self.x, self.v

    def reset(self):
        self.x = 0.0
        self.v = 0.0


# ──────────────────────────────────────────────────────────────
#  APLICACIÓN: INTERFAZ GRÁFICA + ANIMACIÓN
# ──────────────────────────────────────────────────────────────

class SimulatorApp:
    """
    Conecta el controlador difuso con la planta y la interfaz gráfica.

    Optimizaciones de rendimiento:
    - Historial en deque (O(1) append/pop vs O(n) de list.pop(0))
    - Arrays numpy internos para evitar conversión en cada frame
    - Límites de ejes con umbral de cambio para evitar redraws innecesarios
    - 2 pasos de simulación por frame (más fluido sin lag)
    """

    HISTORY  = 300   # puntos de historial visibles
    STEPS_PER_FRAME = 2   # pasos de simulación por callback de animación

    def __init__(self):
        self.ctrl  = FuzzyMRAController()
        self.plant = MRASystem()

        self.running = False
        self.t       = 0.0
        self.xd      = 1.0

        # Historial como deque: append y popleft son O(1)
        self.hist_t = deque(maxlen=self.HISTORY)
        self.hist_x = deque(maxlen=self.HISTORY)
        self.hist_e = deque(maxlen=self.HISTORY)
        self.hist_F = deque(maxlen=self.HISTORY)
        self.hist_v = deque(maxlen=self.HISTORY)

        # Arrays numpy espejo del deque (actualizados en _animate)
        self._arr_t = np.zeros(0)
        self._arr_x = np.zeros(0)
        self._arr_e = np.zeros(0)
        self._arr_F = np.zeros(0)
        self._arr_v = np.zeros(0)

        # Límites anteriores de ejes (para actualizar solo cuando cambian)
        self._prev_xlim = (0.0, 1.0)
        self._prev_ylim_pos   = (-1.0, 2.0)
        self._prev_ylim_err   = (-5.0, 5.0)
        self._prev_ylim_force = (-45.0, 45.0)
        self._prev_ylim_phase = (-3.0, 3.0)
        self._prev_xlim_phase = (-2.0, 2.0)
        self._frame_count = 0

        self._build_figure()
        self._connect_widgets()

    # ── Construcción de la figura ─────────────────────────────
    def _build_figure(self):
        self.fig = plt.figure(figsize=(16, 9))
        self.fig.patch.set_facecolor('#1a1a2e')
        plt.rcParams.update({'text.color': 'white'})

        gs_main = GridSpec(
            2, 2, figure=self.fig,
            left=0.06, right=0.97, top=0.94, bottom=0.32,
            wspace=0.35, hspace=0.55, width_ratios=[1.1, 1]
        )
        style = dict(facecolor='#16213e')

        self.ax_pos   = self.fig.add_subplot(gs_main[0, 0], **style)
        self.ax_err   = self.fig.add_subplot(gs_main[1, 0], **style)
        self.ax_anim  = self.fig.add_subplot(gs_main[0, 1], **style)
        self.ax_phase = self.fig.add_subplot(gs_main[1, 1], **style)

        gs_force = GridSpec(
            1, 2, figure=self.fig,
            left=0.06, right=0.97, top=0.30, bottom=0.22, wspace=0.35
        )
        self.ax_force = self.fig.add_subplot(gs_force[0, 0], facecolor='#16213e')

        self._style_axes()
        self._init_lines()
        self._init_animation_objects()
        self._build_widgets()

        self.fig.suptitle(
            'Simulador Masa-Resorte-Amortiguador  ·  Control Difuso Mamdani',
            color='white', fontsize=13, fontweight='bold', y=0.98
        )

    def _style_axes(self):
        axes_cfg = [
            (self.ax_pos,   'Posicion  x(t)  [m]',        'Tiempo [s]'),
            (self.ax_err,   'Error  e(t)  [m]',            'Tiempo [s]'),
            (self.ax_force, 'Fuerza de control  F(t)  [N]','Tiempo [s]'),
            (self.ax_phase, 'Plano de fase',                'x [m]'),
            (self.ax_anim,  'Animacion del sistema',        ''),
        ]
        for ax, title, xlabel in axes_cfg:
            ax.set_title(title, color='#e0e0e0', fontsize=9, pad=4)
            ax.set_xlabel(xlabel, color='#aaaaaa', fontsize=8)
            ax.tick_params(colors='#aaaaaa', labelsize=7)
            for sp in ax.spines.values():
                sp.set_edgecolor('#444466')
            ax.grid(True, color='#2a2a4a', linewidth=0.5)

        self.ax_phase.set_ylabel("x'  [m/s]", color='#aaaaaa', fontsize=8)
        self.ax_anim.set_xticks([])
        self.ax_anim.set_yticks([])
        self.ax_anim.grid(False)

    def _init_lines(self):
        self.line_x,  = self.ax_pos.plot([], [], color='#4fc3f7', lw=1.5, label='x(t)')
        self.line_xd, = self.ax_pos.plot([], [], color='#69f0ae', lw=1.2, ls='--', label='xd')
        self.ax_pos.legend(loc='upper right', fontsize=7,
                           facecolor='#1a1a2e', labelcolor='white')

        self.line_e,  = self.ax_err.plot([],   [], color='#ef5350', lw=1.5)
        self.line_F,  = self.ax_force.plot([], [], color='#ffb74d', lw=1.5)
        self.line_ph, = self.ax_phase.plot([], [], color='#ce93d8', lw=1.0, alpha=0.7)
        self.dot_ph,  = self.ax_phase.plot([], [], 'o', color='#ff4081', ms=6, zorder=5)

    # ── Animación física 2D ───────────────────────────────────
    def _init_animation_objects(self):
        ax = self.ax_anim
        ax.set_xlim(-0.5, 5.5)
        ax.set_ylim(-1.5, 1.5)
        ax.set_aspect('equal')

        # Pared fija
        ax.add_patch(mpatches.Rectangle((-0.5, -1.5), 0.3, 3.0,
                                         color='#546e7a', zorder=1))
        for y in np.linspace(-1.4, 1.4, 8):
            ax.plot([-0.5, -0.1], [y, y + 0.3], color='#37474f', lw=0.8, zorder=2)

        # Resorte (zigzag actualizable)
        self.spring_line, = ax.plot([], [], color='#80cbc4', lw=2, zorder=3)

        # Amortiguador
        self.damper_body = mpatches.Rectangle((0, -0.25), 0.8, 0.5,
                                               color='#546e7a', zorder=3)
        ax.add_patch(self.damper_body)
        self.damper_rod, = ax.plot([], [], color='#b0bec5', lw=3, zorder=4)
        self.damper_hatches = [
            ax.plot([], [], color='#37474f', lw=1, zorder=4)[0] for _ in range(4)
        ]

        # Masa
        self.mass_patch = mpatches.FancyBboxPatch(
            (2.0, -0.5), 0.8, 1.0, boxstyle="round,pad=0.05",
            facecolor='#5c6bc0', edgecolor='#9fa8da', lw=1.5, zorder=5
        )
        ax.add_patch(self.mass_patch)
        self.mass_text = ax.text(2.4, 0.0, 'M', color='white',
                                  ha='center', va='center',
                                  fontsize=9, fontweight='bold', zorder=6)

        # Setpoint
        self.sp_dot, = ax.plot([], [], 'o', color='#69f0ae', ms=10, zorder=7, label='xd')
        ax.legend(loc='upper right', fontsize=7, facecolor='#1a1a2e', labelcolor='white')
        ax.axhline(-0.55, color='#455a64', lw=1.5, zorder=1)

        self._px0    = 1.2
        self._pscl   = 0.6
        self._wall_x = -0.2

        # Precomputar el patrón zigzag normalizado (no depende de la posición)
        n_coils, amp = 8, 0.18
        n_pts = n_coils * 2 + 2
        self._spring_t = np.linspace(0., 1., n_pts)
        self._spring_y = np.zeros(n_pts)
        for i in range(1, n_pts - 1):
            self._spring_y[i] = amp if i % 2 == 1 else -amp

    # ── Widgets ───────────────────────────────────────────────
    def _build_widgets(self):
        color_slider = '#16213e'

        sl_specs = [
            ('m',  [0.07, 0.17, 0.18, 0.025],  0.5, 10.0, 1.0,  'kg'),
            ('k',  [0.07, 0.13, 0.18, 0.025],  1.0, 50.0, 10.0, 'N/m'),
            ('b',  [0.07, 0.09, 0.18, 0.025],  0.1, 10.0, 1.0,  'Ns/m'),
            ('xd', [0.07, 0.05, 0.18, 0.025], -3.0,  3.0, 1.0,  'm'),
        ]
        self.sliders = {}
        for name, pos, vmin, vmax, v0, unit in sl_specs:
            ax_sl = self.fig.add_axes(pos, facecolor=color_slider)
            sl = Slider(ax_sl, f'{name} [{unit}]', vmin, vmax, valinit=v0,
                        color='#5c6bc0',
                        handle_style={'facecolor': '#9fa8da', 'size': 8})
            sl.label.set_color('#aaaaaa');  sl.label.set_fontsize(8)
            sl.valtext.set_color('#e0e0e0'); sl.valtext.set_fontsize(8)
            self.sliders[name] = sl

        ax_br = self.fig.add_axes([0.40, 0.09, 0.10, 0.055], facecolor='#1a1a2e')
        ax_bx = self.fig.add_axes([0.52, 0.09, 0.10, 0.055], facecolor='#1a1a2e')
        self.btn_run = Button(ax_br, 'Iniciar',   color='#2e7d32', hovercolor='#43a047')
        self.btn_rst = Button(ax_bx, 'Reiniciar', color='#6a1b9a', hovercolor='#8e24aa')
        for btn in (self.btn_run, self.btn_rst):
            btn.label.set_color('white')
            btn.label.set_fontsize(9)
            btn.label.set_fontweight('bold')

        info_ax = self.fig.add_axes([0.68, 0.04, 0.28, 0.15], facecolor='#16213e')
        info_ax.set_xticks([]); info_ax.set_yticks([])
        for sp in info_ax.spines.values():
            sp.set_edgecolor('#444466')
        self.info_text = info_ax.text(
            0.05, 0.5, '', transform=info_ax.transAxes,
            color='#e0e0e0', fontsize=8, va='center', fontfamily='monospace'
        )

    def _connect_widgets(self):
        self.sliders['m'].on_changed(self._on_param_change)
        self.sliders['k'].on_changed(self._on_param_change)
        self.sliders['b'].on_changed(self._on_param_change)
        self.sliders['xd'].on_changed(lambda _: setattr(self, 'xd', self.sliders['xd'].val))
        self.btn_run.on_clicked(self._on_toggle)
        self.btn_rst.on_clicked(self._on_reset)

    def _on_param_change(self, _):
        self.plant.m = self.sliders['m'].val
        self.plant.k = self.sliders['k'].val
        self.plant.b = self.sliders['b'].val

    def _on_toggle(self, _):
        self.running = not self.running
        self.btn_run.label.set_text('Pausar' if self.running else 'Reanudar')

    def _on_reset(self, _):
        self.running = False
        self.btn_run.label.set_text('Iniciar')
        self.t = 0.0
        self.plant.reset()
        self.hist_t.clear(); self.hist_x.clear()
        self.hist_e.clear(); self.hist_F.clear(); self.hist_v.clear()
        self._frame_count = 0

    # ── Animación física (resorte precomputado) ───────────────
    def _update_animation(self, x_pos: float):
        px = float(np.clip(self._px0 + x_pos * self._pscl, 0.3, 4.8))

        # Resorte: escalar el patrón precomputado al largo actual
        sx = self._wall_x + self._spring_t * (px - self._wall_x)
        sy = self._spring_y + 0.35
        self.spring_line.set_data(sx, sy)

        # Amortiguador
        body_x = self._wall_x + (px - self._wall_x) * 0.3
        self.damper_body.set_x(body_x)
        self.damper_rod.set_data([body_x + 0.8, px], [-0.35, -0.35])
        for i, h in enumerate(self.damper_hatches):
            rx = body_x + 0.15 + i * 0.17
            if rx < body_x + 0.8:
                h.set_data([rx, rx], [-0.5, -0.2])
            else:
                h.set_data([], [])

        # Masa y setpoint
        self.mass_patch.set_x(px)
        self.mass_text.set_x(px + 0.4)
        sp_px = float(np.clip(self._px0 + self.xd * self._pscl, 0.3, 4.8))
        self.sp_dot.set_data([sp_px + 0.4], [0.0])

    # ── Actualización de plots con umbrales ───────────────────
    def _update_plots(self):
        t = self._arr_t
        x = self._arr_x
        e = self._arr_e
        F = self._arr_F
        v = self._arr_v
        n = len(t)

        if n < 2:
            return

        t_min, t_max = float(t[0]), float(t[-1])

        # ── Posición ──
        self.line_x.set_data(t, x)
        self.line_xd.set_data([t_min, t_max], [self.xd, self.xd])
        xlim = (t_min, max(t_max, t_min + 1.0))
        if xlim != self._prev_xlim:
            self.ax_pos.set_xlim(*xlim)
            self.ax_err.set_xlim(*xlim)
            self.ax_force.set_xlim(*xlim)
            self._prev_xlim = xlim

        x_lo = min(float(x.min()), self.xd) - 0.4
        x_hi = max(float(x.max()), self.xd) + 0.4
        if abs(x_lo - self._prev_ylim_pos[0]) > 0.05 or \
           abs(x_hi - self._prev_ylim_pos[1]) > 0.05:
            self.ax_pos.set_ylim(x_lo, x_hi)
            self._prev_ylim_pos = (x_lo, x_hi)

        # ── Error ──
        self.line_e.set_data(t, e)
        e_lo = float(e.min()) - 0.2
        e_hi = float(e.max()) + 0.2
        if abs(e_lo - self._prev_ylim_err[0]) > 0.05 or \
           abs(e_hi - self._prev_ylim_err[1]) > 0.05:
            self.ax_err.set_ylim(e_lo, e_hi)
            self._prev_ylim_err = (e_lo, e_hi)

        # ── Fuerza ──
        self.line_F.set_data(t, F)
        F_lo = float(F.min()) - 1.0
        F_hi = float(F.max()) + 1.0
        if abs(F_lo - self._prev_ylim_force[0]) > 0.5 or \
           abs(F_hi - self._prev_ylim_force[1]) > 0.5:
            self.ax_force.set_ylim(F_lo, F_hi)
            self._prev_ylim_force = (F_lo, F_hi)

        # ── Plano de fase ──
        self.line_ph.set_data(x, v)
        self.dot_ph.set_data([x[-1]], [v[-1]])
        px_lo = float(x.min()) - 0.3
        px_hi = float(x.max()) + 0.3
        pv_lo = float(v.min()) - 0.3
        pv_hi = float(v.max()) + 0.3
        if abs(px_lo - self._prev_xlim_phase[0]) > 0.1 or \
           abs(px_hi - self._prev_xlim_phase[1]) > 0.1:
            self.ax_phase.set_xlim(px_lo, px_hi)
            self._prev_xlim_phase = (px_lo, px_hi)
        if abs(pv_lo - self._prev_ylim_phase[0]) > 0.1 or \
           abs(pv_hi - self._prev_ylim_phase[1]) > 0.1:
            self.ax_phase.set_ylim(pv_lo, pv_hi)
            self._prev_ylim_phase = (pv_lo, pv_hi)

        # ── Cuadro de información (cada 3 frames para no thrashear) ──
        if self._frame_count % 3 == 0:
            self.info_text.set_text(
                f"  t   = {self.t:7.2f} s\n"
                f"  x   = {self.plant.x:+7.4f} m\n"
                f"  x'  = {self.plant.v:+7.4f} m/s\n"
                f"  e   = {float(e[-1]):+7.4f} m\n"
                f"  F   = {float(F[-1]):+7.4f} N\n"
                f"  m={self.plant.m:.1f}  k={self.plant.k:.1f}  b={self.plant.b:.1f}"
            )

    # ── Callback principal de animación ──────────────────────
    def _animate(self, _frame: int):
        """
        FuncAnimation llama aquí cada ~20 ms.
        Ejecuta STEPS_PER_FRAME pasos de simulación para mayor fluidez.
        """
        if not self.running:
            return []

        # Avanzar N pasos por frame (la simulación avanza más rápido que el reloj)
        for _ in range(self.STEPS_PER_FRAME):
            x = self.plant.x
            v = self.plant.v
            e  = self.xd - x
            de = -v
            F  = self.ctrl.infer(e, de)

            self.plant.step(F)
            self.t += self.plant.dt

            self.hist_t.append(self.t)
            self.hist_x.append(self.plant.x)
            self.hist_e.append(e)
            self.hist_F.append(F)
            self.hist_v.append(v)

        self._frame_count += 1

        # Convertir deques a arrays numpy solo cuando hay datos nuevos
        self._arr_t = np.fromiter(self.hist_t, dtype=float, count=len(self.hist_t))
        self._arr_x = np.fromiter(self.hist_x, dtype=float, count=len(self.hist_x))
        self._arr_e = np.fromiter(self.hist_e, dtype=float, count=len(self.hist_e))
        self._arr_F = np.fromiter(self.hist_F, dtype=float, count=len(self.hist_F))
        self._arr_v = np.fromiter(self.hist_v, dtype=float, count=len(self.hist_v))

        self._update_plots()
        self._update_animation(self.plant.x)

        return []

    # ── Lanzador ─────────────────────────────────────────────
    def run(self):
        """Inicia la animación y muestra la ventana."""
        self.anim = animation.FuncAnimation(
            self.fig, self._animate,
            interval=20,
            blit=False,
            cache_frame_data=False
        )
        plt.show()


# ──────────────────────────────────────────────────────────────
#  PUNTO DE ENTRADA
# ──────────────────────────────────────────────────────────────

if __name__ == '__main__':
    app = SimulatorApp()
    app.run()
