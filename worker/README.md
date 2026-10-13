# colszunet

## 配置账号

打开项目根目录的 `config.json`，填写 `username` 和 `password`。

`domain` 留空时使用普通账号。账号包含认证域时，可以填写例如 `@cmcc`。

脚本会省略 `get_challenge` 的 `ip` 参数，等待门户返回 `client_ip`，再使用门户分配的地址完成认证和在线状态确认。配置文件中的地址不需要填写。

## 启动监控

在项目根目录执行：

```powershell
python colszunet.py
```

程序每隔 `probe_interval_seconds` 秒访问 `connectivity_test_url`。连续 `failure_threshold` 次访问失败时，程序执行一次校园网登录，并确认外部网站访问已经恢复。

监控程序会为连通性检测和门户接口请求创建显式直连 opener，不继承系统代理访问校园网。进程启动时会在当前进程内增加 `NO_PROXY` 的校园域名排除项，同时记录实际代理环境，日志中不会写入账号密码。

监控程序使用 Windows 单实例锁。发现已有监控进程时，新的进程会记录原因并以退出代码 `3` 结束。

运行日志保存到脚本同目录的 `colszunet.log`，日志文件达到约 2 MB 后自动轮换，最多保留 5 个历史文件。日志会记录进程标识、Python 与 OpenSSL 版本、请求目标、直连策略、请求编号、HTTP 状态、响应大小、耗时以及 `ssl_error`、`timeout`、`connection_reset` 等错误类型。

## 当前配置

- 连续失败次数：`3`
- 连通性测试地址：`https://www.baidu.com/`
- 门户地址：`https://net.szu.edu.cn`
- `ac_id`：`12`

如果校园网要求验证码，同时填写 `captcha_id` 和 `captcha_value`。
