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

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 产仔登记（配对完成即建档）

配对批准后，`complete` 动作不再只记一串编号，而是当场为每只幼崽建立 `animal` 档案，并把配对当前的公兽/母兽写成 `sire_id`/`dam_id`：

```json
POST /api/entities/<pairing_id>/actions
{
  "action": "complete",
  "data": {"offspring": [
    {"id": "幼崽编号", "name": "姓名", "sex": "male|female|unknown", "birth_date": "YYYY-MM-DD"}
  ]},
  "expected_version": 2
}
```

整批为一个事务：编号在批内重复、编号已有档案、或任一只资料缺失/不合规（姓名、性别、出生日期，出生日期不得晚于今天）时，全部幼崽都不落档，配对仍停在 `approved`。失败响应为 400，`errors` 数组按行号（从 0 开始）列出每只冲突幼崽及原因（`duplicate_in_batch`、`already_archived`、`invalid_sex`、`invalid_birth_date` 等），演示页面会把对应行标红。

成功后可按亲本查整窝后代：

```
GET /api/animals?sire_id=<公兽编号>&dam_id=<母兽编号>
```

两个亲本条件可单独或组合使用；每次建档和配对完成都会写入审计。


## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

谱系系数是简化亲缘规则，不替代专业谱系软件、遗传咨询或法定动物运输许可。
