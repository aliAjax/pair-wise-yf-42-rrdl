# 动物园谱系与繁育协调

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8308`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8308
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `animal`：个体谱系；`pairing`：配对建议；`transfer`：机构和运输记录。

## 产仔登记

对已批准（`approved`）的配对提交 `complete` 动作即完成产仔登记，请求体为：

```json
{"action": "complete", "data": {"offspring": [
  {"id": "baby-1", "name": "团团", "sex": "male", "birth_date": "2026-05-10"}
]}}
```

- 每只幼崽必须提供编号、姓名、性别（`male`/`female`）和出生日期（`YYYY-MM-DD`，不得晚于当天）。
- 登记成功时系统当场为每只幼崽建立动物档案，并自动写入当前配对的公兽（`sire_id`）、母兽（`dam_id`）和来源配对（`pairing_id`），配对流转为 `completed`。
- 编号在批内重复、编号已有档案或任一资料不合规时，整批不落档，配对仍停留在 `approved`；响应为 `400`，`conflicts` 数组按 `index` 标出每只冲突幼崽及原因。
- 按亲本查整窝后代：`GET /api/animals?parent_id=<亲本编号>`（也可用 `sire_id`、`dam_id`、`pairing_id` 过滤）。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。
