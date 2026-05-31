# Backuper

Backuper 是一套面向 Linux 的服务端/客户端文件备份工具。操作入口保持简单：

```bash
./server
./client
```

当前版本是第一版工程骨架，重点完成：

- 服务端交互菜单
- 服务端账号管理，支持随机密钥和手动设置密码/密钥
- 服务端生成 `BKUP1:` 客户端配置码
- 服务端内置 WebDAV 接收服务
- 服务端 WebDAV 认证失败限速、临时封禁和认证失败日志
- HTTP / HTTPS 独立监听开关，关闭代表不使用该端口
- HTTPS 自签证书自动生成
- 服务端可要求 Backuper 专用访问令牌，拒绝浏览器等普通 WebDAV 请求
- 服务端云同步可读取本机 rclone remote
- 服务端云同步支持云端相对路径和自动同步计划
- 客户端导入服务端配置码
- 客户端保留服务器连接手动配置、修改、查看和测试入口
- 客户端支持多个服务端连接，每个备份任务绑定其中一个服务端
- 客户端全局备份计划
- 客户端多目录任务，按顺序串行执行
- 客户端备份目录管理按现有任务列表和序号操作
- 客户端任务列表显示每个任务上一次备份结果
- 每个目录可选择打包上传或同步，任务名就是服务端上的相对根目录
- 每个任务支持忽略特定文件后缀和顶层目录
- 打包模式使用高压缩率，并在上传前本地校验归档可读
- 客户端后台服务默认以 root 运行，便于读取不同用户拥有的文件
- 打包模式通过 tar 保留 numeric owner、ACL 和 xattrs
- 同步模式额外上传文件 uid/gid/mode 元数据清单
- 客户端非敏感配置导入/导出
- rclone 限速、重试和并发参数

## 依赖

服务端：

- Python 3.10+
- openssl，用于自动生成 HTTPS 自签证书

客户端：

- Python 3.10+
- rclone
- tar

## 快速开始

### 服务端一键安装


使用方法

```bash
curl -fsSL https://raw.githubusercontent.com/smmya/backuper/main/install-server.sh | sudo bash
```

默认安装到执行命令时所在目录下的 `backuper` 子目录，会安装系统依赖、初始化服务端配置、生成 `backuper-server.service` 并启动后台服务。后续管理可以使用：

```bash
cd ./backuper && ./server
```

也可以直接运行：

```bash
backuper-server
```

可选环境变量：

- `BACKUPER_REPO_URL`: GitHub 仓库地址，例如 `https://github.com/<owner>/<repo>.git`
- `BACKUPER_BRANCH`: 分支名，默认 `main`
- `BACKUPER_BASE_DIR`: 安装基准目录，默认执行命令时所在目录；程序会安装到该目录下的 `backuper`
- `BACKUPER_INSTALL_DIR`: 完整安装目录；设置后会覆盖 `BACKUPER_BASE_DIR`
- `BACKUPER_AUTO_START`: 是否安装后立即启动 systemd 服务，默认 `1`
- `BACKUPER_INSTALL_RCLONE`: 是否安装 rclone，默认 `1`

服务端：

```bash
chmod +x server client
./server
```

建议流程：

1. 初始化/修复配置
2. 基础设置中确认 HTTP / HTTPS 监听开关和端口
3. 账号管理中创建账号
4. 生成客户端配置码
5. 前台启动接收服务，或生成 systemd 配置后后台运行

云同步：

1. 先在服务器上用 `rclone config` 配置好云存储 remote
2. 进入 `./server` 的“云同步设置”
3. 从本机 rclone 配置中选择 remote
4. 输入云端相对路径，例如 `backuper/server1`
5. 设置自动同步计划，或手动执行立即同步

如果 systemd 服务以 root 运行，而你是在普通用户下配置的 rclone，菜单会记录当前 rclone 配置文件路径，后台同步时会通过 `--config` 使用该文件。

客户端：

```bash
./client
```

建议流程：

1. 服务器连接配置，粘贴服务端 `BKUP1:` 配置码
2. 全局备份计划，设置一轮备份的触发时间
3. 备份目录管理，添加多个目录任务
4. 传输参数设置，配置限速和重试
5. 立即执行一轮备份测试

后台服务在客户端的“后台服务/开机自启”子菜单中管理，支持自动安装/修复 systemd 开机自启、启动、停止、重启，以及删除开机自启配置。

关于文件用户/用户组：

- `archive` 打包模式会把原始 uid/gid、权限、ACL、扩展属性写入 tar 包；恢复时用 root 解包即可还原。
- `sync` 同步模式受 WebDAV/rclone 协议限制，远端文件本身无法直接变成客户端原始 uid/gid，所以客户端会额外上传 `.backuper/metadata/<任务名>-metadata.json`，服务端收到后会按清单对任务目录执行 `chown/chmod`。

远端目录规则：

- 客户端任务名必须唯一。
- 客户端没有单独的远端目录字段。
- 服务端保存路径为 `storage/{username}/{task_name}/`。
- 打包模式直接把版本归档放在该目录下，例如 `storage/alice/etc/etc-20260530-220000.tar.zst`。
- 同步模式的数据直接放在 `storage/alice/etc/`。
- 同步模式元数据放在 `storage/alice/.backuper/metadata/etc-metadata.json`。

客户端服务器连接既可以导入服务端配置码，也可以手动输入：

- HTTP / HTTPS
- 服务器地址和端口
- 账号
- 密码/密钥
- WebDAV 远端路径
- Backuper 程序访问令牌
- TLS 校验和 CA 证书

多服务端：

- 客户端可以保存多个服务端连接。
- 每个备份任务绑定一个服务端。
- 添加或修改任务时可以选择任务使用哪个服务端。

忽略规则：

- 后缀忽略会匹配所有文件名后缀，例如 `.tmp`、`.log`。
- 顶层目录忽略只匹配备份根目录下的第一层目录名，例如备份 `/opt` 时忽略 `abc` 表示忽略 `/opt/abc`，不会因为深层路径里也叫 `abc` 就忽略。

普通请求访问限制：

- 服务端默认要求 `X-Backuper-Access` 请求头。
- 服务端生成客户端配置码时会把程序访问令牌写入配置码。
- 客户端通过 rclone 上传/列目录时会自动附带该请求头。
- 浏览器或普通 WebDAV 客户端没有该令牌时会被拒绝。

## 配置文件

运行后会生成：

```text
var/config/server.json
var/config/client.json
var/logs/client.log
storage/
```

认证失败日志：

```text
var/logs/server-auth.log
```

同一来源 IP 连续认证失败会被延迟响应，达到阈值后会临时封禁。成功登录后会清除该 IP 的失败计数。

客户端导出的配置不包含：

- 用户名
- 密钥
- 服务器绑定信息

它只包含：

- 全局备份计划
- 备份任务列表
- 传输参数
- 失败处理策略

## 说明

服务端 WebDAV 目前是内置最小实现，用于配合 rclone 上传、同步、删除和目录列表。后续可以继续增强锁、配额、访问日志、正式证书和云同步定时器。
