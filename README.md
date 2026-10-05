# Gerber Revision Review 208

接收单层 ASCII Gerber 光绘文件, 重建实际铜层轮廓, 输出面积/包围盒/连通块/孔洞统计, 并从同一最终几何生成 SVG 下载; 亦可一次上传顶/底两份 Gerber 与 JSON 网表做双层板导电关系核对, 或再附 JSON 制造规则做铜间距与禁铜区审查(只报告, 不裁铜或自动修板)。纯后端, 无前端页面。

## 模块分工

- `copper_rev208/parser.py` — 词法/语法解析, 输出事件流(含原文行号定位)
- `copper_rev208/geometry.py` — 曝光几何引擎: 圆弧离散、光圈缓冲、区域填充、LPD/LPC 按序合成(Shapely)
- `copper_rev208/netcheck.py` — 双层网表核对: 孔盘扣除、铜岛提取、镀铜孔连通图、短/断路报告
- `copper_rev208/drc.py` — 制造规则审查: 规则 JSON 校验、同层铜间距量测、分层禁铜区接触检查
- `copper_rev208/svg_export.py` — 最终几何 → SVG(Y 轴翻转、evenodd 孔洞)
- `main.py` — FastAPI 请求处理: 上传、参数校验、错误响应、结果暂存

## 支持的 Gerber 子集

- `%FSLAXnnYnn*%` 前导零省略 + 绝对坐标; `%MOMM*%` / `%MOIN*%`
- `%ADDnnC,d*%` 圆形、 `%ADDnnR,xXy*%` 轴对齐矩形(仅闪光), 均无孔
- `Dnn` 选光圈; `D01` 绘制 / `D02` 移动 / `D03` 闪光; X/Y 省略沿用当前位置
- `G01` 直线; `G75` 下 `G02`/`G03` 顺/逆圆弧(跨象限、整圆, I/J 为相对起点的圆心偏移)
- `G36`/`G37` 单个简单直线闭环区域, 按内部填充
- `%LPD*%` 加铜 / `%LPC*%` 扣除, 严格按指令顺序组合, 后续 LPD 可恢复先前清除
- `G04` 注释、 `M02` 结束(缺失视为截断; 末尾 M02 缺 `*` 结束符同样定位并拒绝)

未定义光圈、非法圆弧、未闭合/自交区域、未支持指令、截断文件均返回 HTTP 422 及带行号与原文的错误, 不交付部分结果。

## 运行

```bash
.venv/bin/pip install -r requirements.txt   # 环境已备好可跳过
.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
```

## API

### POST /api/rebuild

multipart 上传: `file` = Gerber 文件; `tolerance` = 曲线近似误差(mm, 正数, 默认 0.01)。

```bash
curl -s -F "file=@samples/sample1.gbr" -F "tolerance=0.01" http://127.0.0.1:8000/api/rebuild
```

返回示例:

```json
{
  "area_mm2": 1234.567,
  "bbox_mm": {"min_x": 0, "min_y": 0, "max_x": 30, "max_y": 20, "width": 30, "height": 20},
  "components": 1,
  "holes": 1,
  "svg_id": "...",
  "svg_url": "/api/rebuild/<svg_id>/svg"
}
```

空铜层正常返回(area 为 0, bbox 为 null)。错误返回 422:

```json
{"detail": {"error": "未定义的光圈 D10", "line": 7, "source": "D10"}}
```

### GET /api/rebuild/{svg_id}/svg

下载由同一最终几何生成的 SVG(附件形式, 孔洞以 evenodd 正确表达, Y 轴方向已翻转)。

```bash
curl -OJ http://127.0.0.1:8000/api/rebuild/<svg_id>/svg
```

## 自测
### POST /api/netcheck

multipart 一次上传: `top` = 顶层 Gerber, `bottom` = 底层 Gerber,
`netlist` = JSON 网表文件, `tolerance` = 曲线近似误差(mm, 正数, 默认 0.01)。
两层共用同一板坐标系, 不做镜像。

```bash
curl -s -F "top=@samples/net_top.gbr" -F "bottom=@samples/net_bottom.gbr" \
     -F "netlist=@samples/netlist.json" -F "tolerance=0.01" \
     http://127.0.0.1:8000/api/netcheck
```

网表 JSON 格式(坐标单位 mm):

```json
{
  "terminals": [{"id": "T1", "net": "GND", "layer": "top", "x": 2.0, "y": 2.0}],
  "holes": [{"id": "H1", "x": 5.0, "y": 5.0, "diameter": 1.0, "plated": true}]
}
```

- `terminals[]`: `id` 全局唯一, `net` 网络名, `layer` 为 `top`/`bottom`, `x`/`y` 为毫米坐标
- `holes[]`: `id` 全局唯一, `diameter` 为正, `plated` 表示是否镀铜
- 重复 ID、非有限数值(NaN/Infinity)、重叠或相切的孔均拒绝(422, 含 JSON 内位置)

核对规则: 各层先扣除全部孔盘形成铜岛(同层仅点接触不导通); 镀铜孔壁
连接两层所有沿孔周有正长度接触的铜岛(点接触不算), 非镀铜孔只扣铜,
顶底平面重叠不直接导通, 支持多孔传递连接。端子落在指定层扣孔后的铜岛
(边界算落铜), 多岛交点报歧义; 未落铜端子单列, 不参与短/断路分组。

返回 `networks`(实际网络与端子归属)、`shorts`(异名网络连通, 含铜岛-孔
连接链 `chain`)、`opens`(同网端子分离的端子组 `groups`)、
`unconnected_terminals` 与 `ok`。任何校验失败都不交付部分结果。

### POST /api/drc

multipart 一次上传: `top` = 顶层 Gerber, `bottom` = 底层 Gerber,
`netlist` = JSON 网表, `rules` = JSON 制造规则,
`tolerance` = 曲线近似误差(mm, 正数, 默认 0.01)。
复用扣孔后铜岛与镀铜孔实际网络(顶底平面重叠不算连接)。

```bash
curl -s -F "top=@samples/drc_top.gbr" -F "bottom=@samples/drc_bottom.gbr" \
     -F "netlist=@samples/drc_netlist.json" -F "rules=@samples/drc_rules.json" \
     http://127.0.0.1:8000/api/drc
```

规则 JSON 格式(坐标与距离单位 mm):

```json
{
  "default_clearance_mm": 0.2,
  "clearance_overrides": [{"nets": ["A", "B"], "clearance_mm": 0.5}],
  "keepouts": [{"id": "K1", "layer": "bottom",
                "polygon": [[1.0, 1.0], [2.0, 1.0], [2.0, 2.0], [1.0, 2.0]]}]
}
```

- `default_clearance_mm`: 正数默认铜间距, 必填
- `clearance_overrides[]`: `nets` 为两个网络名(名称对无序), `clearance_mm`
  覆盖默认值; 重复名称对、未知网络名、非有限值、非正阈值均拒绝(422)
- `keepouts[]`: `id` 全局唯一, `layer` 为 `top`/`bottom`, `polygon` 为
  简单多边形顶点序列(支持凹形); 自交与零面积拒绝

审查规则:

- 铜间距只量同层、不同实际网络铜岛的最短实体距离(非包围盒/端子距离),
  同实际网络免检; 距离小于阈值才违规, 等于允许。多名称实际网络取全部
  组合适用覆盖阈值的最大值, 无端子实际网络用默认。
- 禁铜区与扣孔后铜(含浮铜)任何接触均违规; 区域位于孔内且不碰铜则通过。

返回 `clearance_violations`(层、铜岛、实际网络与设计网络名、阈值、
实测距离、一对最近点)、`keepout_violations`(区域 ID、铜岛、接触位置)、
`netcheck`(短/断路报告)与 `ok`。空层与无违规正常返回。

```bash
.venv/bin/python -m pytest tests -q
.venv/bin/python -m compileall -q copper_rev208 main.py tests
```

样例见 `samples/`: `sample1.gbr`(区域+闪光+走线+圆弧+LPC 开孔后恢复)、`sample2.gbr`(英寸单位+整圆+顺时针圆弧); 网表核对样例 `net_top.gbr`/`net_bottom.gbr`/`netlist.json`(镀铜孔连接顶底、非镀铜孔扣铜、SIG 跨层断路、GND/VCC 经 H1 短路、T5 未落铜); 规则审查样例 `drc_top.gbr`/`drc_bottom.gbr`/`drc_netlist.json`/`drc_rules.json`(顶层 A/B 铜块 0.3mm 间距触发 0.5mm 覆盖阈值, 底层浮铜触碰禁铜区 K1)。

Repository: https://github.com/huangjie666777-ux/pcb-revision-review-208
