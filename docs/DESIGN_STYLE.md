
## 2. CORE DESIGN PHILOSOPHY: "Spatial Glass"
**Visual Style:** A hybrid of **macOS Fluent Design**, **Windows 11 Mica**, and **intentional neumorphism**—but rendered purely via PySide6 widgets (NOT QML). The app should feel:
- **Lightweight:** Instant startup (<200ms to first paint).
- **Tactile:** Buttons press. Switches slide. Lists have inertia.
- **Respectful:** No modal spam. No "Are you sure?" for reversible actions.
- **OLED-friendly:** True dark mode with semantic elevation, not just #000.

**Anti-Bloat Manifesto (Learn from competitors):**
- **Anti-Discord:** Never nest more than 2 panels deep. Use context-aware sidebars that collapse intelligently.
- **Anti-TeamSpeak:** No cryptic tree-hierarchy for navigation. Use human-readable "Spaces" with iconography.
- **Anti-Telegram:** No hidden gestures as primary navigation. Every gesture must have a visible affordance.
- **Anti-ICQ:** No feature creep. If a feature needs a tutorial, redesign it or kill it.

---

## 3. TECHNICAL STACK & CONSTRAINTS
- **Framework:** PySide6 (Qt 6.5+). Pure `QWidget` application. No QML. No WebEngine.
- **Styling:** `qdarkstyle` is banned. Use a custom `QPalette` + `QSS` injection system with CSS-like variables.
- **Animation:** All motion via `QPropertyAnimation`, `QParallelAnimationGroup`, `QEasingCurve`. No `QTimer`-based hacks.
- **Graphics:** `QPainter` for custom widgets. `QGraphicsDropShadowEffect` used sparingly (max 1 per view, blur=20, color at 15% opacity).
- **Performance:** Target 60fps. Animate only `transform` and `opacity` equivalents (`geometry`, `windowOpacity`, `pos`). No layout recalculations during animation.

---

## 4. WINDOW ARCHITECTURE & TRANSITIONS
The app uses a **"Stacked Deck"** paradigm. There is ONE `QMainWindow`. All views are `QWidget` pages managed by a custom `SlidingStackedWidget`.

### Transition Types (MANDATORY):
1. **Cross-Fade:** For equal-hierarchy navigation (e.g., Settings tabs). Duration: 180ms. Easing: `QEasingCurve.Type.OutCubic`.
2. **Horizontal Slide:** For drill-down navigation (e.g., Chat List → Chat Room). Duration: 250ms. Easing: `QEasingCurve.Type.InOutQuart`. The outgoing view slides left at 0.9 scale + fade; incoming slides from right at 1.0 scale.
3. **Vertical Push:** For modals/overlays (e.g., User Profile). Duration: 300ms. Easing: `QEasingCurve.Type.OutBack` (slight overshoot for playfulness).
4. **Morph:** For contextual actions (e.g., FAB → Compose window). Animate shared elements' geometry between states.

### Window Chrome:
- **Frameless** but **native-respecting**. Custom titlebar on Windows/Linux; respects macOS traffic lights via `Qt.WindowType.CustomizeWindowHint` + platform detection.
- **Snap Layouts:** Windows 11 snap assist must work. Handle `WM_NCHITTEST` correctly or use `Qt.WindowType.FramelessWindowHint` with native shadow margins.

---

## 5. LAYOUT SYSTEM: "The Three Zones"
Avoid Discord's "panel hell." Use a **dynamic tri-zone** layout:

| Zone | Behavior | Content |
|------|----------|---------|
| **Rail (64px)** | Always visible. Icon-only vertical bar. Hover expands to 240px with text labels (200ms slide). | Spaces, DM, Settings |
| **Canvas (Fluid)** | Main content area. Changes density based on window width (responsive breakpoints at 800px, 1200px). | Chat, Settings Panels, Discovery |
| **Palette (320px max)** | Contextual sidebar. Auto-hides on narrow windows. Slide-in from right. | Member list, Thread list, User Info |

**Responsive Rules:**
- < 800px: Rail becomes bottom tab bar (mobile-compact mode, but still desktop app).
- 800-1200px: Rail + Canvas. Palette is a slide-over overlay.
- > 1200px: Full tri-zone. Palette docks.

---

## 6. COMPONENT LIBRARY (Custom Widgets)
Build these as reusable `QWidget` subclasses with `paintEvent`:

### 6.1. `GlassButton`
- Background: `rgba(255,255,255,0.06)` with 1px border `rgba(255,255,255,0.1)`.
- Hover: Background brightens to `0.12`, subtle lift shadow appears.
- Press: Scale to 0.97 + shadow collapses.
- **Corner Radius:** 8px (consistent across ALL components).

### 6.2. `ChannelPill`
- Replaces Discord's channel list. Looks like a pill, not a tree item.
- Unread state: Left accent border (3px) + subtle background glow. No red dot spam.
- Active state: Full background fill, bold text.
- Hover: `x+4px` translate (gentle nudge).

### 6.3. `MessageBubble`
- No hard borders. Subtle elevation difference.
- Grouped messages from same user: Only last bubble has tail. Avatars align to last bubble.
- Hover reveals action bar (Reply, React, More) with **staggered fade-in** (each icon 40ms delay).

### 6.4. `ContextualAppBar`
- Replaces static top bars. Appears when text is selected or items are multi-selected.
- Material: Blur-behind (`QGraphicsBlurEffect` on a screenshot of background? No—use translucent `QWidget` with `Qt.WindowType.WindowStaysOnTopHint` inside parent).
- Animation: Drop from top with `OutBack`, 200ms.

---

## 7. COLOR & TYPOGRAPHY SYSTEM
**Palette (Dark Mode Default):**
```python
# Semantic colors
SURFACE_0 = "#0B0B0F"      # Deepest background
SURFACE_1 = "#121218"      # Cards, panels
SURFACE_2 = "#1C1C24"      # Elevated hover states
SURFACE_3 = "#272730"      # Active inputs
ACCENT = "#7C6BFF"         # Primary action (periwinkle—distinct from Discord blurple)
ACCENT_GLOW = "rgba(124,107,255,0.25)"  # For shadows/highlights
TEXT_PRIMARY = "#F0F0F5"
TEXT_SECONDARY = "#8E8E99"
SUCCESS = "#34D399"
DANGER = "#FB7185"
Typography:
Use system font stack: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Ubuntu.
No font size smaller than 12px. Respect user OS accessibility settings by querying QFontDatabase and scaling.
```

8. MICRO-INTERACTIONS & FEEDBACK
The "Living UI" Rules:
Every action has a reaction. Click a copy button? It morphs into a checkmark for 1.5s, then gracefully morphs back.
Skeleton screens, not spinners. Loading content shows shimmering gradient boxes (via QPropertyAnimation on QLinearGradient stop points), never a spinning wheel.
Typing indicator. Three dots that don't just blink—they smoothly morph through a wave (scale + opacity offset).
Voice Activity. Not a static icon. A radial pulse around the avatar that reacts to audio volume (decibel-mapped QPropertyAnimation on a QPainter ring).
New Message. Chat doesn't just appear. It slides up from 8px below + fade, 150ms. If off-screen, the scroll thumb subtly pulses.
9. NAVIGATION & DISCOVERABILITY (Fixing Telegram's sins)
Command Palette: Ctrl+K opens a spotlight-style overlay (blurred backdrop). Search across spaces, DMs, settings, actions. This is the primary power-user tool.
Predictable Shortcuts: Ctrl+1..9 for Rail items. Ctrl+Shift+T for last DM. Esc always backs out or closes overlays.
No hidden swipes. If a sidebar can be swiped, there is a 3px visible handle with a hover hint.
Onboarding: Not a slideshow. Use contextual coach marks—arrows pointing to actual UI the first time a feature is relevant.
10. ACCESSIBILITY & NATIVE INTEGRATION
High Contrast: Respect QGuiApplication.styleHints().colorScheme(). Support forced-colors mode.
Screen Readers: All custom widgets implement QAccessibleInterface. No QLabel abuse for buttons.
Native Notifications: Use winrt (Windows), pync (macOS), notify2 (Linux). Never custom notification popups unless OS denies permission.
Titlebar: On Windows, implement Mica/Acrylic via DwmSetWindowAttribute calls if possible; fallback to solid SURFACE_1.