---
name: music-songwriting
description: Help the user write, revise, critique, and produce original songs with the Music Agent tools.
---

用户提出写歌、改词、押韵、调整副歌或评价歌词时，调用 `music_lyric_coach`。将用户要求和已有歌词作为对话消息传入；mode 使用 draft（创作）、revise（修改）或 critique（点评）。根据返回内容向用户说明修改思路并展示歌词。

歌词展示后先邀请用户确认或提出修改。只有用户明确确认最终歌词、主题、曲风和时长后，才调用 `music_create_generation`，传入已确认内容。创建后用 `music_get_generation` 检查状态；若仍在排队或生成中，告知任务 ID 并稍后再查。成功后提供返回的 audio_url。未经用户确认，不要创建音乐生成任务。

歌词工具用 DeepSeek 辅助创作；确认后的歌词歌曲由用户自部署的 ACE-Step API 生成，纯音乐可由本地合成器生成。不要声称本地纯音乐合成器会唱歌词。如果 ACE-Step 未连接或生成失败，告知用户检查其自部署服务状态，不要改用其他音乐供应商。
