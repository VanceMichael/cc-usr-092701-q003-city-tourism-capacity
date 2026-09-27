# 城市文旅活动容量调度

本项目保存城市文旅活动容量调度所需的领域上下文和校验契约，便于服务端功能围绕真实业务参与方展开。当前版本只提供资料读取、结构校验和命令行摘要，数据均为演示用虚构内容。

## 参与方

文旅调度员、景区工作人员、旅行社、游客服务人员

## 事实资料

- 北京中秋假期接待游客878.3万人次，旅游总花费115.4亿元
- 游客接待量靠前的区域包含公园、商圈和历史文化景区
- 假期举办了演出、游园、艺术周和京郊丰收节等多类活动

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 编译

```bash
python3 -m compileall -q src tests
```

## 命令行检查

```bash
python3 -m src.city_tourism_capacity.context fixtures/context.json
```
