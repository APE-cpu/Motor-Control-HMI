# 页面插图

2026-09-27 使用内置 image_gen 工具分别生成四张 1536×1024 PNG，生成模式 generate。
原始图片直接复制到本目录；Qt 在运行时等比缩放、裁切并用主题背景渐隐，无离线改图。
这些是装饰插图，不表示当前电机的真实结构或测量数据。

| 文件 | 页面 | 元素 |
|---|---|---|
| vector.png | 矢量可视化 | 转子端面、定子绕组、磁场光弧 |
| fourier.png | 离线傅里叶 | 铜绕组特写、波形纹理 |
| identify.png | 参数辨识 | 同轴电机零件拆解 |
| twin.png | 数字孪生 | 实体到线框的电机 |

## 公共提示词

Use case: illustration-story. Asset type: production background art for the Chinese motor-control desktop app 驭衡智控. Generate one wide 1536x1024 landscape bitmap, no UI, no text, no letters, no numbers, no labels, no charts, no watermark. A realistic high-end industrial illustration with machined dark satin steel, precise copper windings, faint icy-cyan field highlights, nearly black graphite background. The object occupies the RIGHT TWO THIRDS, with a smooth dark fade to pure dark negative space on the LEFT THIRD. The whole object should remain recognizable at a display size of 400x280. Restrained lighting, crisp silhouette, no bright lens flare, no purple or green, no smoke. Match a coherent family of motor-engineering application backgrounds.

## 各图追加提示词

### vector.png

A close-up end-face of a permanent magnet motor, rotor hub slightly right of center, clearly separated steel rotor and copper stator coils, concentric machining marks, restrained cyan flux arcs surrounding rotor. No drawn coordinate axes or annotations. The rotor extends a little beyond the right frame edge.

### fourier.png

A diagonal macro view of a stator section with copper winding bundles and dark laminated steel teeth, clearly distinct from a complete circular motor. Thin, subtle blue and amber flowing wave ribbons curve along the lower right margin, decorative with no graph axes. Copper texture and detailed winding geometry are the main visual.

### identify.png

An exploded axial electric motor assembly in three-quarter view, laid out horizontally across the right two thirds: separated front bearing endcap, copper wound stator cylinder, metal rotor on a single coaxial shaft, rear bearing and casing. All components aligned to one mechanical axis with believable proportions. Clean separated components, no leader lines or numbers, no extra unrelated parts.

### twin.png

A complete compact industrial electric motor in three-quarter view, its front and shaft rendered as realistic satin metal, transitioning smoothly across the rear half into a delicate cyan wireframe mesh. Motor mounted on a faint dark simulation floor grid at lower right. A clear single motor with an integral solid-to-wireframe transition, not two motors.

## 集成

`widgets/page_artwork.py` 将插图叠放在整页底层，按尺寸、DPI、主题缓存绘制结果。
结构面板和绘图区适度透明，输入框及按钮保持不透明；页面不为插图预留列或页眉。
`main_window.py` 保留原页面对象与页面索引，在滚动容器内包裹插图框架。
`build_exe.bat` 的整个 assets 目录打包规则包含这些文件。
导航图标在 `widgets/nav_icons.py` 中以 SVG 路径定义，不属于生成位图。

## 零件重组动画（2026-09-28）

`motor_parts.png`：内置 image_gen 的 generate + background-extraction edit 输出，保留原始 alpha 通道。
2×2 图集顺序为前盖、转子轴、铜绕组定子、后壳。Qt 在运行时读取各零件区域，沿同一投影轴拆分和合拢。
这是二维分层装饰动画，不是当前电机的三维 CAD 装配仿真。

进入插图页播放约 1000 ms：展开 → 合拢 → 融入目标背景；参数辨识页停留在展开造型后融入拆解背景。
背景重绘最多约 30 Hz，动画结束、页面隐藏、连续切页或窗口关闭时立即停止。
操作控件保持静止，动画层不接收鼠标事件。

### 图集生成提示词

Use case: product-mockup. Production sprite sheet for a motor assembly animation in a dark industrial desktop application. Generate a 1536x1024 transparent PNG, TRUE alpha transparent background, no checkerboard drawn, no floor, no shadows on a backdrop, no text, no labels, no borders. Exactly FOUR isolated electric motor components laid out in a strict 2x2 grid, each component fully inside its own 768x512 quadrant with generous transparent padding. All four share the identical orthographic three-quarter camera view: shaft axis horizontal with front facing lower left and rear upper right, moderate 20 degree elevation. Top left: a dark satin steel circular front bearing endplate with a round central bore, isolated. Top right: a metallic permanent magnet rotor with a long polished coaxial shaft protruding both ends, isolated. Bottom left: an open copper-wound cylindrical stator with visible central bore, copper coil bundles and dark laminated steel, isolated. Bottom right: a hollow dark steel finned outer motor housing with rear endcap and mounting feet, opening facing lower left, isolated. Consistent relative mechanical proportions and realistic precision machining, restrained cyan rim light, subtle copper warm highlights. Each component occupies 75 percent of its quadrant width, centers exactly (384,256),(1152,256),(384,768),(1152,768). There must be absolutely no touching or overlap between quadrants. Beautiful engineering product render, no wires, no extra components.

### 透明图层修订提示词（最终采用）

Use case: background-extraction. EDIT this existing 1536x1024 four-component motor sprite sheet. Remove ALL background completely to genuine alpha=0 transparency, including the gray glow around all four parts and the dark areas inside the open central bores. Keep ONLY the four solid motor parts with clean alpha edges. Preserve EXACT pixel positions, dimensions, 2x2 quadrant layout, proportions, lighting, materials and the geometry of every component. DO NOT add a background, DO NOT paint a checkerboard, DO NOT add floor or shadows. It is for compositing separate moving sprites onto an app UI, so true alpha transparency is essential. Output PNG with a real transparent alpha channel.
