# OCR / 数字识别

> 读游戏里的**数值**（体力、金币、剩余次数、商店刷新花费……），
> 用来做「够不够 / 还差多少」这类预算判断。

一句话：**用你自己采的 10 张数字字形做模板匹配，不用通用 OCR。**

- 代码：`e7bot/ocr.py`（`DigitReader` / `TextReader` / `OcrConfig`）
- 配置：`config/default.toml` 的 `[ocr]` 段
- 自检：`python run.py doctor`（`[ocr] enabled = true` 时会报还缺哪几个字形）
- 测试：`pytest tests/test_ocr.py -q`

---

## 1. 为什么是「采字形 + 模板匹配」

本项目有一条硬规则：**模板必须从真实客户端采集，不预置任何 UI 图**。
数字识别遵守同一条规则，理由不只是"保持一致"：

| 维度 | 采字形模板（本项目） | 通用 OCR（PaddleOCR / Tesseract / …） |
|---|---|---|
| 准确度 | 游戏数字是**固定点阵字体**，同一数字在不同界面是同一张字形，相关系数实测 **0.84~1.00**，最大误配 0.72 | 面对「亮字暗底 + 描边 + 半透明底 + 描边发光」的 HUD 容易翻车，8/3、6/5 混淆是常态 |
| 依赖 | 零新增（只用已有的 cv2 / numpy） | onnxruntime + 模型，几十 MB |
| 速度 | 一次读数约 **16 ms**（原生分辨率） | 一次推理几十到几百 ms，还要占内存 |
| 换语言 | 韩/日/繁客户端数字字形一样，不用动 | 要换模型或重训 |
| 分辨率 | 把字形缩放到模板尺寸再比，**天生分辨率无关** | 一般也能，但小字号下掉点更多 |

所以 `[ocr] backend = "digits"` 是默认且推荐的后端。
`backend = "rapidocr"` 那个可选后端是给「要读中文/英文文本」的场景留的（见第 7 节）。

---

## 2. 采集数字字形

### 2.1 放哪、叫什么

```
templates/<profile>/ocr/digits/
├── 0.png ~ 9.png     必需 —— 缺一个 is_available() 就是 False
├── k.png             可选，"1.2k" 的千
├── m.png             可选，"3.5m" 的百万
├── dot.png           可选，小数点（文件名不能叫 ".png"）
└── slash.png         可选，斜杠（"12/20"）
```

每个 PNG 建议配一个同名 `.json`（`{"ref_size": [1920, 1080]}`），
跟其它模板一致、方便统一管理。**注意**：字形比较用的是 PNG 的**原生像素**，
不看 `ref_size`，所以这个字段填错也不影响识别。

### 2.2 怎么采

用现成的采集器就行（`python run.py capture`，GUI 里"填名字 → 框选 → 保存"）：

1. 进到游戏里有数字的界面（体力条、金币、商店刷新花费都行）；
2. 名字栏填 `ocr/digits/0`，框选那个数字 `0` 的**紧致外框**，保存；
   → 落到 `templates/default/ocr/digits/0.png`（旁边的 `.json` 由采集器自动写）
3. 换界面/换数字，把 `1` ~ `9` 依次采齐；
4. 顺手把 `1.2k` 里的 `.` 采成 `ocr/digits/dot`、`k` 采成 `ocr/digits/k`
   （还有 `m`、`12/20` 里的 `/` → `slash`）—— 这些是可选的，缺了只影响带后缀的读数；
5. `python run.py doctor`：`[ocr] enabled = true` 时会告诉你还缺哪几个字形。

> 一位数字在游戏里往往凑不齐 0-9 —— 多跑几个界面（背包数量、商店价格、圣域
> 体力、结算金币）就能凑齐。**同一个数字在不同界面字形是一样的**，采哪处都行。

### 2.3 采集要点（直接决定识别稳不稳）

1. **框紧到墨迹**：多带背景会让相似度整体偏低，甚至把两个数字的差距抹平。
   本模块**不会**自动帮你裁掉多余背景（试过，反而会在框得很紧的字形上裁掉笔画）。
2. **每个字形单独一张图**，不要把整串数字存成一张。
3. **按你实际游玩的分辨率采最好**；按 1920×1080 采也能跨分辨率用（见第 6 节实测）。
4. **别采动态效果下的字形**：数字在跳动画（掉血、金币滚动）时采到的可能是半透明的
   中间帧。等它停下来再采。
5. 采完**换一两个界面回验**：用第 4 节的调试片段跑一遍，confidence 应当 ≥ 0.85。

---

## 3. 配 region

`region` 是**相对游戏窗口客户区的归一化坐标** `(x, y, w, h)`，全部 0~1 —— 与项目
其它地方（模板的搜索区、点击坐标）完全一致，换分辨率不用改。

```toml
# 例：某界面右下角体力数字
[my_task]
stamina_region = [0.62, 0.905, 0.085, 0.045]
```

三条硬要求：

1. **必须留背景边距**（四周各留 15%~30%）。识别链路靠「前景像素占比是否超过一半」
   自动判断亮字暗底还是暗字亮底；region 紧贴到只剩墨迹时这个判据会失效。
2. **只框数字本身**，不要带图标、按钮边框、别的文字。
3. **别跨行**。同一行多个识别框会被拼接，跨行会被当成多个字形串成一个数。

### 怎么快速找到坐标

用截图 + 归一化换算，肉眼确认：

```python
import cv2, numpy as np
from e7bot.winutil import Rect

# 一张窗口客户区截图（宽度 = 客户区宽度）
frame = cv2.imdecode(np.fromfile("shot.png", dtype=np.uint8), cv2.IMREAD_COLOR)
r = Rect(0, 0, frame.shape[1], frame.shape[0])

# 把你在图上量到的**像素**框换算成归一化 region
px, py, pw, ph = 1190, 978, 163, 49          # 像素坐标
region = (px / r.width, py / r.height, pw / r.width, ph / r.height)
print("region =", tuple(round(v, 4) for v in region))

# 把 region 裁出来存盘，肉眼确认框对没对
sub = r.sub(*region)
cv2.imencode(".png", frame[sub.top:sub.bottom, sub.left:sub.right])[1].tofile("region.png")
```

`region.png` 里应当是**干净的一行数字 + 一点背景**。框歪了就把像素坐标改一改重跑。

---

## 4. 接进任务

### 4.1 最省事：引擎已经接好了

`e7bot/engine.py` 里有现成的封装，任务里直接用就行：

```python
class MyTask:
    def run(self, bot):
        # 只要数值（读不出来返回 None）
        stamina = bot.read_number(self.stamina_region)
        if stamina is None:
            return False          # 读不出来就**别猜**，交给上层保守处理
        if stamina < 30:
            bot.log.info("体力不够，回大厅")
            return True

        # 要原始文本（"12/20" 这类）
        raw = bot.read_text(self.stamina_region)
```

`bot.read_number()` 在 `[ocr] enabled = false`、字形不全、读数失败这三种情况下都
返回 `None`（并在日志里说明原因），任务侧只要处理 `None` 即可，不会抛异常。

### 4.2 要 confidence / 逐字形分数

```python
from e7bot.ocr import make_digit_reader

reader = make_digit_reader(bot.matcher, bot.cfg.section("ocr"))
if reader.is_available():                     # 0-9 齐备
    img, rect = bot.frame()
    reading = reader.read(img, rect, self.stamina_region)
    if reading is not None:
        bot.log.info("体力 %d（原始 %r，可信度 %.2f）",
                     reading.value, reading.raw, reading.confidence)
    else:
        bot.log.warning("体力读不出来，缺字形: %s", reader.missing_glyphs())
```

三种读法：

| 方法 | 返回 | 用在哪 |
|---|---|---|
| `read(frame, rect, region)` | `NumberReading` 或 `None` | **主力**。任何不可信都返回 `None` |
| `read_text(...)` | `"3480"` / `"1.2k"` / `"34?0"` 或 `None` | 打日志、排查"哪个字形没认出来" |
| `read_pair(...)` | `(12, 20)` 或 `None` | `"12/20"` 形式的数值对（剩余次数） |

`NumberReading` 字段：

```python
reading.value       # 1200      —— "1.2k" 已经换算好
reading.raw         # "1.2k"    —— 原始识别文本
reading.confidence  # 0.0~1.0   —— 取所有字形分数的最小值（最弱一环）
reading.digits      # [('1',0.97), ('.',0.98), ('2',0.96), ('k',0.95)]
reading.region      # 你传进去的 region
```

**`read()` 的保守性**：只要有一个字形判为 `?`（所有候选都低于阈值），整体就返回
`None`，绝不返回"部分结果"。原因很直接 —— 把 `3480` 读成 `348` 比读不出来危险得多
（脚本会以为体力还够，继续刷）。所以调用方必须处理 `None`。

### `[ocr]` 配置段

```toml
[ocr]
enabled = false          # run.py doctor 是否检查字形
backend = "digits"       # "digits"（推荐）| "rapidocr"
digits_prefix = "ocr/digits"
threshold = 0.80         # 相似度阈值；读不出来先看第 5 节再考虑调它
scale_tolerance = 0.06   # 允许字形比模板大/小 6%，低分辨率下有用
```

---

## 5. 读不出来怎么办

按这个顺序排查（**先怀疑输入，再怀疑阈值**）：

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `read_text()` 返回 `None` | region 里一个字形都没分割出来 | region 框歪了 / 数字还没渲染出来 / 画面全黑 |
| `read_text()` 有 `?` | 那个位置的字形没认出来 | 看 `?` 的位置对应哪个字符，去补采那个字形（`dot.png` 最常缺） |
| `doctor` 报缺字形 | 0-9 没采全 | 补采，或先只跑不需要读数的功能 |
| 全部字形都接近阈值 | region 带太多背景 / 字形采得太松 | 重新框紧一点采字形 |
| 数字在跳动 | 采到动画中间帧 | 等数字停下再读，或把读取时机挪到界面稳定后 |
| 换了分辨率后读不出来 | 降到比采集分辨率低很多 | 见第 6 节 |

调阈值是**最后**手段，而且要知道代价：阈值越低，字母/噪点被当成数字的风险越高。
真要调，一次降 0.02~0.05，然后用第 4 节的片段在多个界面上回验。

---

## 6. 实测数据与已知限制

数据来自 `tests/test_ocr.py` 的合成画面（`cv2.putText` 画数字 → 切成单字字形当模板
→ 缩放到目标分辨率后读）。**合成字体比真实游戏字体更"方"**，真实客户端的数字通常
更粗、更花哨，实测分数一般不会比这更差，但请以你自己采集后的回验为准。

### 跨分辨率（模板按 1920×1080 采）

| 画面分辨率 | `3480` | `1.2k` | `3.5m` | `12/20` |
|---|---|---|---|---|
| 1920×1080 | 0.91 | 0.97 | 0.95 | 0.96 |
| 2560×1440 | 0.97 | 0.95 | 0.95 | 0.96 |
| 3840×2160 | 0.96 | 0.87 | 0.87 | 0.96 |
| 1600×900 | 0.94 | 0.85 | 0.83 | 0.96 |
| 1366×768 | 0.83 | 0.83 | 0.81 | 0.96 |
| 1280×720 | 0.83 | 0.84 | 0.75 | 0.96 |
| 1024×576 | ❌ 读不出 | 0.77 | ❌ 读不出 | ❌ 读不出 |

（表中是 `confidence`，阈值 0.80。`12/20` 一列是"能正确读出数值对"）

### 已知限制（如实列出）

1. **放大很轻松，缩得很小就吃力**。1920×1080 采的字形在 1440p/4K 下几乎无损；
   缩到 720p 时分数掉到 0.75~0.84（阈值 0.80），**能读但余量不大**；
   缩到 576p 基本读不出来。
   - 长期在低分辨率玩 → **按那个分辨率采字形**，或者把 `threshold` 降到 0.75。
2. **小数点是最脆弱的一个**。它在 720p 下只有 6×6 像素，与自己的模板相关度只有
   0.75（数字是 0.83+）。模块对"高度不到本行最高字形一半"的小字形自动把阈值放宽
   0.10（`_threshold_for`），就是为了救它；误配上限实测 0.50，放宽是安全的。
3. **形状本来就一样的字符分不开**：`O` 会被读成 `0`、`B` 会被读成 `8`。
   数字栏里不会出现字母，所以实际影响可以忽略；但别拿它去读含字母的文本
   （那种场景用第 7 节的 `TextReader`）。
4. **读不出来时会慢一些**：正常读数约 16 ms；某个字形没过阈值会触发一遍精细搜索
   （尺寸网格 + 模糊对齐），单次读数约 **150 ms**，整片噪声区域最坏约 250 ms。
   所以别在高频循环里对着一片空白区反复读。
5. **不支持多行**。一个 region 只应当框一行数字。

### 算法要点（想改代码时先看这里）

```
裁 ROI -> 灰度 -> Otsu 二值化 + 自动反色（保证"字形=白"）-> 开运算去噪
       -> 垂直投影分割字形（过窄段只在缝隙 <= 8% 行高时合并，否则小数点会被吞掉）
       -> 每个字形缩放到模板尺寸 -> 与 0-9 等候选逐个比相似度
       -> 拼接 -> 解析 "3480" / "1.2k" / "3.5m"
```

- **分割用二值图、比较用灰度图**：小字形二值化后几乎是一块实心方块，相关系数会
  失去分辨力（实测 `1.2k` 的点只能拿 0.49 分，判为读不出来）；改用灰度后同一个点
  拿到 0.97。所以两张图各司其职。
- **相似度取绝对值**：模板可能是亮字也可能是暗字，相关系数在整体反色时正好取负，
  取绝对值等于"极性无关"，省掉一次容易误判的极性判断。
- **两级匹配**：先用便宜的一遍比全部候选；只有没过阈值的字形才做精细搜索
  （宽高各 ±3px 的尺寸网格 + 让模板也走一遍字形那侧的降采样）。
  这一手把 720p 下的 `8` 从 0.74 拉到 0.83。

---

## 7. 可选：通用文本 OCR（读中文/英文）

`TextReader` 用 `rapidocr_onnxruntime`，能读中文/英文文本（商品名、任务标题等）。
它**刻意做成可选**：库不存在时 `is_available()` 返回 `False`、所有读取方法返回
`None`，绝不抛异常 —— 脚本运行中不能因为缺一个可选库就崩。

```powershell
# 需要时才装（约几十 MB，含 onnxruntime）
.\.venv\Scripts\python.exe -m pip install rapidocr_onnxruntime
```

```python
text = make_text_reader(cfg.section("ocr"))
if text.is_available():
    print(text.read_text(frame, rect, region))    # "誓约书签"
    print(text.read_number(frame, rect, region))  # 从 "3,480 G" 里抠出 3480
else:
    # 没装就安静跳过；真需要"必须有 OCR"的地方可以显式 text.require() 拿安装提示
    ...
```

读数字**优先用 `DigitReader`**：它对点阵数字更准、更快，也不依赖任何额外安装。
`TextReader` 留给"必须读一段文字"的场景。

---

## 8. 相关文件

| 文件 | 作用 |
|---|---|
| `e7bot/ocr.py` | `DigitReader` / `TextReader` / `OcrConfig` / 工厂 |
| `e7bot/engine.py` | `bot.read_number(region)` / `bot.read_text(region)` 封装（懒加载 + 吞异常） |
| `tests/test_ocr.py` | 合成画面下的真实测试（含分辨率无关、亮暗底、缺字形降级） |
| `e7bot/vision.py` | 模板库与匹配器（字形模板从这里的 `matcher.lib` 取） |
| `config/default.toml` | `[ocr]` 段 |
| `templates/default/README.md` | 模板库总说明与命名规范 |
