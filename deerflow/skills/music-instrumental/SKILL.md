---
name: music-instrumental
description: Collaborate with the user to plan and create original instrumental music through ACE-Step or the local Music Agent synthesizer.
---

用户提出创作纯音乐时，先帮助梳理主题、曲风、情绪、乐器和时长；缺少关键信息时简短追问。把方案整理后展示给用户。

用户确认生成后，调用 `music_create_instrumental`，传入 theme、style、instrument（piano、guzheng、music_box、guitar、synth_pad）、duration_seconds（10 到 120 秒）和 generator（默认 `ace-step`；用户要求本地程序合成时设为 `local`）。ACE-Step 纯音乐使用 `[inst]` 生成标记并按所选乐器描述生成。任务创建后使用 `music_get_generation` 查询状态；排队或生成中时告知任务 ID，稍后查询；成功时提供 audio_url。

用户试听后提出修改时，先用自己的话复述具体调整方向并征求确认。确认后调用 `music_create_revision`，传入成功的上一版 job_id 和用户反馈。新任务会继承主题、曲风、乐器及时长并记录父版本。用 `music_get_generation` 查询新版本；用户要求对比时调用 `music_list_versions` 并展示版本、修改意见和试听链接。不要覆盖或删除旧版本。

ACE-Step 模式由模型生成完整录音；本地模式通过程序编曲并用 GM 音源渲染。DeepSeek 密钥若已配置，可协助本地模式生成逐小节编曲；未配置时使用本地规则。不要传歌词，也不要声称会合成人声。
