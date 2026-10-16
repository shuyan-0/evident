# 推送到 GitHub · 操作指南

> 你选择了「自己在本地推」。本指南配合 `evident_with_git.tar.gz`（含完整 git 历史）使用。
> 全程约 3 分钟，无需告诉我任何 Token。

---

## 第 1 步：在 GitHub 上新建仓库

1. 打开 https://github.com/new
2. **Repository name** 填：`evident`
3. 可见性选 **Public** 或 Private（提交作业建议 Public）
4. ⚠️ **不要勾选** "Add a README file"、"Add .gitignore"、"Choose a license"（勾了会产生冲突）
5. 点 **Create repository**

> 也可以直接用你 @ 提到的名字 `Yyy1357`，效果一样。

---

## 第 2 步：下载并解压源码包

下载 `evident_with_git.tar.gz`，然后解压：

```bash
tar xzf evident_with_git.tar.gz
cd evident
```

包内**已包含完整 git 仓库**（5 次提交、main 分支），无需重新 `git init`。

---

## 第 3 步：推送（二选一）

### 方式 A：用现成脚本（最省事）

把下载的 `push-to-github.sh` 放到 `evident` 同级目录，然后：

```bash
bash push-to-github.sh <你的Token> evident
```

### 方式 B：手动命令

```bash
cd evident

# 若之前配过别的远程，先清掉
git remote remove origin 2>/dev/null || true

# 换成你自己的仓库地址
git remote add origin https://github.com/shuyan-0/evident.git

git push -u origin main
```

推送时会提示输入用户名和密码：
- **用户名**：`shuyan-0`
- **密码**：粘贴你的 **Personal Access Token**（不是登录密码！）

---

## 关于 Token（GitHub 已禁用密码认证）

如果你还没有 Token：

1. 打开 https://github.com/settings/tokens
2. 点 **Generate new token** → 选 **Tokens (classic)**
3. 勾选权限：**`repo`**（完整仓库读写）
4. 有效期建议选 30 天
5. 生成后**立即复制**（离开页面就看不到了）
6. 推送到 Git 提示密码时，粘贴这个 Token

> 若用 Git Credential Manager 或 IDE 推送，通常会自动弹浏览器授权，就不用手动建 Token。

---

## 推送完成后验证

浏览器打开 `https://github.com/shuyan-0/evident`，应能看到：

- ✅ 5 次提交记录
- ✅ README.md 自动渲染
- ✅ `app/`（源码）、`docs/`（AI记录+测试说明）、`tests.py`、`static/` 等 17 个文件

---

## 常见问题

**Q：提示 `repository not found`**
A：仓库名或用户名写错了，或 Token 没有 `repo` 权限。

**Q：提示 `failed to push some refs`**
A：新建仓库时勾选了 README/.gitignore。解决：
```bash
git pull --rebase origin main
git push -u origin main
```
或删掉远程仓库重建（**不要勾选任何初始化文件**）。

**Q：不想暴露邮箱**
A：仓库设置里开启 "Keep my email addresses private"，或在 `.git/config` 里把
user.email 改成 `shuyan-0@users.noreply.github.com`。

**Q：Token 会泄漏吗？**
A：不会。`.git/config` 里的 remote 地址若用脚本方式写入了 Token，建议推送成功后执行
`git remote set-url origin https://github.com/shuyan-0/evident.git` 清除。
