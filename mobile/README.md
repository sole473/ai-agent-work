# Music Agent 原生客户端

该目录是 Android 和 iOS 共用的 Expo React Native 项目。客户端连接已部署的 Music Agent HTTPS 地址，DeepSeek 密钥始终保留在服务器上。

## 配置服务地址

在本机 Windows PowerShell 中，进入 `mobile` 目录后复制示例配置。将地址改为手机可访问的 Music Agent HTTPS 地址。

```powershell
# 进入原生客户端项目目录。
Set-Location 'B:\music-agent\mobile'

# 从示例创建本机配置文件。该文件不应提交到 Git。
Copy-Item .env.example .env
```

## 安装和本机预览

需要先安装 Node.js LTS，它会一同提供 `npm`。随后在 Windows PowerShell 中运行：

```powershell
# 进入原生客户端项目目录。
Set-Location 'B:\music-agent\mobile'

# 根据 package.json 下载 Expo、React Native 和原生音频依赖。
npm install

# 检查 Expo 依赖版本是否匹配。无错误表示依赖可用于启动。
npx expo-doctor

# 启动开发服务器。终端显示二维码后，可用 Android Expo Go 扫描预览。
npx expo start
```

## 构建原生安装包

构建前需要登录 Expo 账号。Android 预览包会输出 APK。iOS 内部测试包和商店包需要 Apple 开发者账号及其签名配置。

```powershell
# 进入原生客户端项目目录。
Set-Location 'B:\music-agent\mobile'

# 登录用于云端构建的 Expo 账号。
npx eas-cli@latest login

# 构建可直接安装到 Android 设备的预览 APK。
npx eas-cli@latest build --platform android --profile preview

# 构建 iOS 内部测试包。Expo 会在构建过程中引导配置 Apple 签名凭据。
npx eas-cli@latest build --platform ios --profile preview
```

构建完成后，EAS 会提供下载或分发链接。生产发布应使用 `production` 配置，并在 Google Play Console 与 App Store Connect 中提交审核。
