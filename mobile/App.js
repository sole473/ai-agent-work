import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ActivityIndicator,
  Alert,
  Pressable,
  SafeAreaView,
  ScrollView,
  StatusBar,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
} from "react-native";
import { setAudioModeAsync, useAudioPlayer, useAudioPlayerStatus } from "expo-audio";

const API_BASE_URL = (process.env.EXPO_PUBLIC_MUSIC_AGENT_URL || "https://120.53.14.254").replace(/\/$/, "");

const instruments = [
  ["piano", "钢琴"],
  ["guzheng", "古筝"],
  ["music_box", "八音盒"],
  ["guitar", "木吉他"],
  ["synth_pad", "氛围合成器"],
];

const models = [
  ["deepseek-flash", "DeepSeek Flash"],
  ["deepseek-v4-pro", "DeepSeek V4 Pro"],
];
const generatorChoices = [
  ["ace-step", "ACE-Step 模型"],
  ["local", "本地合成"],
];

const sourceLabels = { deepseek: "DeepSeek 编曲", local: "本地规则编曲" };
const statusLabels = { queued: "排队中", running: "生成中", succeeded: "已完成", failed: "生成失败" };

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers: { "Content-Type": "application/json", ...options.headers },
    ...options,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = Array.isArray(data.detail)
      ? data.detail.map((item) => item.msg).join("；")
      : data.detail;
    throw new Error(detail || `请求失败（${response.status}）`);
  }
  return data;
}

function ChoiceChips({ label, choices, value, onChange }) {
  return (
    <View style={styles.field}>
      <Text style={styles.label}>{label}</Text>
      <View style={styles.chips}>
        {choices.map(([choice, title]) => (
          <Pressable
            accessibilityRole="radio"
            accessibilityState={{ selected: value === choice }}
            key={choice}
            onPress={() => onChange(choice)}
            style={[styles.chip, value === choice && styles.chipSelected]}
          >
            <Text style={[styles.chipText, value === choice && styles.chipTextSelected]}>{title}</Text>
          </Pressable>
        ))}
      </View>
    </View>
  );
}

function AudioPreview({ url }) {
  const player = useAudioPlayer(url ? { uri: url } : null);
  const playback = useAudioPlayerStatus(player);
  if (!url) return null;
  return (
    <Pressable
      accessibilityRole="button"
      onPress={() => (playback.playing ? player.pause() : player.play())}
      style={styles.audioButton}
    >
      <Text style={styles.audioButtonText}>{playback.playing ? "暂停试听" : "试听作品"}</Text>
    </Pressable>
  );
}

function GenerationCard({ job }) {
  const source = job.instrumental && job.generator === "local" && job.status === "succeeded"
    ? sourceLabels[job.arrangement_source] || "编曲来源未知"
    : "";
  return (
    <View style={styles.jobCard}>
      <Text style={styles.jobTitle}>{job.theme}</Text>
      <Text style={styles.jobDetail}>
        {[statusLabels[job.status] || job.status, job.generator, job.style, instruments.find(([value]) => value === job.instrument)?.[1], job.duration_seconds == null ? "自动时长" : `${job.duration_seconds} 秒`, source]
          .filter(Boolean)
          .join(" · ")}
      </Text>
      {job.error ? <Text style={styles.error}>{job.error}</Text> : null}
      <AudioPreview url={job.audio_url ? `${API_BASE_URL}${job.audio_url}` : null} />
    </View>
  );
}

export default function App() {
  const [theme, setTheme] = useState("");
  const [style, setStyle] = useState("");
  const [instrument, setInstrument] = useState("piano");
  const [duration, setDuration] = useState("30");
  const [instrumentalGenerator, setInstrumentalGenerator] = useState("ace-step");
  const [model, setModel] = useState("deepseek-flash");
  const [deepThink, setDeepThink] = useState(true);
  const [webSearch, setWebSearch] = useState(true);
  const [message, setMessage] = useState("");
  const [messages, setMessages] = useState([]);
  const [brief, setBrief] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [loadingChat, setLoadingChat] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [loadingJobs, setLoadingJobs] = useState(false);

  const chatHistory = useMemo(
    () => messages.map(({ role, content }) => ({ role, content })).slice(-20),
    [messages],
  );

  const loadJobs = useCallback(async () => {
    setLoadingJobs(true);
    try {
      const page = await request("/generations?limit=20");
      setJobs(page.items);
    } catch (error) {
      Alert.alert("无法读取生成记录", error.message);
    } finally {
      setLoadingJobs(false);
    }
  }, []);

  useEffect(() => {
    setAudioModeAsync({ playsInSilentMode: true, shouldPlayInBackground: true });
    request("/creative-chat/config")
      .then((config) => setModel(config.model))
      .catch(() => {});
    request("/generations/config")
      .then((config) => { if (!config.ace_step_instrumental_available) setInstrumentalGenerator("local"); })
      .catch(() => setInstrumentalGenerator("local"));
    loadJobs();
    const timer = setInterval(loadJobs, 4000);
    return () => clearInterval(timer);
  }, [loadJobs]);

  function selectModel(nextModel) {
    setModel(nextModel);
    if (nextModel !== "deepseek-flash") setWebSearch(false);
  }

  async function sendMessage() {
    const content = message.trim();
    if (!content || loadingChat) return;
    const nextMessages = [...messages, { role: "user", content }];
    setMessages(nextMessages);
    setMessage("");
    setLoadingChat(true);
    try {
      const response = await request("/creative-chat", {
        method: "POST",
        body: JSON.stringify({
          messages: nextMessages.map(({ role, content: text }) => ({ role, content: text })).slice(-20),
          chat_type: "instrumental",
          model,
          deep_think: deepThink,
          web_search: webSearch,
        }),
      });
      setMessages((current) => [...current, { role: "assistant", content: response.reply }]);
      setBrief(response.brief);
    } catch (error) {
      setMessages((current) => [...current, { role: "assistant", content: `暂时无法回复。${error.message}` }]);
    } finally {
      setLoadingChat(false);
    }
  }

  async function createGeneration(values = { theme, style, instrument, duration }) {
    const seconds = Number(values.duration);
    if (!values.theme.trim()) {
      Alert.alert("请填写主题", "先描述想要生成的音乐。");
      return;
    }
    if (!Number.isInteger(seconds) || seconds < 10 || seconds > 120) {
      Alert.alert("时长无效", "时长需要在 10 到 120 秒之间。");
      return;
    }
    setSubmitting(true);
    try {
      const job = await request("/generations", {
        method: "POST",
        body: JSON.stringify({
          theme: values.theme.trim(),
          style: values.style.trim(),
          instrument: values.instrument,
          duration_seconds: seconds,
          instrumental: true,
          instrumental_generator: instrumentalGenerator,
          draft_lyrics: false,
          lyrics: "",
        }),
      });
      setJobs((current) => [job, ...current.filter((item) => item.id !== job.id)]);
      setTheme(values.theme);
      setStyle(values.style);
      setInstrument(values.instrument);
      setDuration(String(values.duration));
      Alert.alert("任务已提交", "生成完成后会自动出现在作品记录中。");
    } catch (error) {
      Alert.alert("提交失败", error.message);
    } finally {
      setSubmitting(false);
    }
  }

  function useBrief() {
    if (!brief) return;
    createGeneration({
      theme: brief.theme,
      style: brief.style,
      instrument: brief.instrument,
      duration: String(brief.duration_seconds),
    });
  }

  return (
    <SafeAreaView style={styles.safeArea}>
      <StatusBar barStyle="dark-content" />
      <ScrollView contentContainerStyle={styles.container} keyboardShouldPersistTaps="handled">
        <View style={styles.hero}>
          <Text style={styles.overline}>MUSIC AGENT</Text>
          <Text style={styles.heading}>音乐工作台</Text>
          <Text style={styles.subheading}>把灵感整理成可试听的原创纯音乐。</Text>
        </View>

        <View style={styles.card}>
          <View style={styles.sectionHeader}>
            <Text style={styles.sectionTitle}>创作助手</Text>
            <Text style={styles.caption}>DeepSeek 协助</Text>
          </View>
          <View style={styles.chatArea}>
            {messages.length === 0 ? <Text style={styles.assistantMessage}>告诉我场景、情绪、参考方向或乐器。我会整理为可生成的纯音乐方案。</Text> : null}
            {messages.map((item, index) => (
              <Text key={`${item.role}-${index}`} style={item.role === "user" ? styles.userMessage : styles.assistantMessage}>
                {item.content}
              </Text>
            ))}
          </View>
          <ChoiceChips label="模型" choices={models} value={model} onChange={selectModel} />
          <View style={styles.switchRow}>
            <Text style={styles.switchLabel}>DeepThink</Text>
            <Switch value={deepThink} onValueChange={setDeepThink} trackColor={{ true: "#7047d9" }} />
            <Text style={styles.switchLabel}>联网搜索</Text>
            <Switch value={webSearch} onValueChange={setWebSearch} disabled={model !== "deepseek-flash"} trackColor={{ true: "#7047d9" }} />
          </View>
          {model !== "deepseek-flash" ? <Text style={styles.caption}>联网搜索需要选择 DeepSeek Flash。</Text> : null}
          <TextInput
            multiline
            onChangeText={setMessage}
            placeholder="例如，雨夜开车穿过城市，想要一点电影感，但不要太悲伤"
            placeholderTextColor="#8a849a"
            style={[styles.input, styles.messageInput]}
            value={message}
          />
          <Pressable disabled={loadingChat} onPress={sendMessage} style={[styles.primaryButton, loadingChat && styles.buttonDisabled]}>
            {loadingChat ? <ActivityIndicator color="#fff" /> : <Text style={styles.primaryButtonText}>发送给创作助手</Text>}
          </Pressable>
          {brief ? <Pressable disabled={submitting} onPress={useBrief} style={styles.secondaryButton}><Text style={styles.secondaryButtonText}>采用方案并生成纯音乐</Text></Pressable> : null}
        </View>

        <View style={styles.card}>
          <Text style={styles.sectionTitle}>直接创作</Text>
          <Text style={styles.label}>主题</Text>
          <TextInput multiline onChangeText={setTheme} placeholder="雨夜回家，窗外有细雨和路灯" placeholderTextColor="#8a849a" style={[styles.input, styles.themeInput]} value={theme} />
          <Text style={styles.label}>曲风</Text>
          <TextInput onChangeText={setStyle} placeholder="华语民谣、木吉他" placeholderTextColor="#8a849a" style={styles.input} value={style} />
          <ChoiceChips label="音色" choices={instruments} value={instrument} onChange={setInstrument} />
          <ChoiceChips label="纯音乐生成器" choices={generatorChoices} value={instrumentalGenerator} onChange={setInstrumentalGenerator} />
          <Text style={styles.label}>时长（秒）</Text>
          <TextInput keyboardType="number-pad" onChangeText={setDuration} style={styles.input} value={duration} />
          <Pressable disabled={submitting} onPress={() => createGeneration()} style={[styles.primaryButton, submitting && styles.buttonDisabled]}>
            {submitting ? <ActivityIndicator color="#fff" /> : <Text style={styles.primaryButtonText}>生成纯音乐</Text>}
          </Pressable>
        </View>

        <View style={styles.card}>
          <View style={styles.sectionHeader}>
            <Text style={styles.sectionTitle}>作品记录</Text>
            <Pressable onPress={loadJobs} style={styles.refreshButton}><Text style={styles.refreshText}>{loadingJobs ? "刷新中" : "刷新"}</Text></Pressable>
          </View>
          {jobs.length ? jobs.map((job) => <GenerationCard job={job} key={job.id} />) : <Text style={styles.caption}>还没有生成记录。</Text>}
        </View>
      </ScrollView>
    </SafeAreaView>
  );
}

const styles = StyleSheet.create({
  safeArea: { flex: 1, backgroundColor: "#f7f6fb" },
  container: { padding: 18, gap: 14 },
  hero: { paddingVertical: 18 },
  overline: { color: "#7047d9", fontSize: 12, fontWeight: "700", letterSpacing: 1.4 },
  heading: { color: "#24213a", fontSize: 34, fontWeight: "800", marginTop: 5 },
  subheading: { color: "#706a7e", fontSize: 15, lineHeight: 22, marginTop: 7 },
  card: { backgroundColor: "#fff", borderColor: "#e7e4ee", borderRadius: 20, borderWidth: 1, gap: 12, padding: 16 },
  sectionHeader: { alignItems: "center", flexDirection: "row", justifyContent: "space-between" },
  sectionTitle: { color: "#292540", fontSize: 18, fontWeight: "700" },
  caption: { color: "#756f83", fontSize: 13, lineHeight: 19 },
  chatArea: { gap: 8, maxHeight: 280 },
  assistantMessage: { alignSelf: "flex-start", backgroundColor: "#f0edfb", borderRadius: 13, color: "#473f5e", lineHeight: 21, padding: 10 },
  userMessage: { alignSelf: "flex-end", backgroundColor: "#6a3bd0", borderRadius: 13, color: "#fff", lineHeight: 21, padding: 10 },
  field: { gap: 7 },
  label: { color: "#4d475d", fontSize: 14, fontWeight: "700" },
  chips: { flexDirection: "row", flexWrap: "wrap", gap: 7 },
  chip: { borderColor: "#ddd8ea", borderRadius: 18, borderWidth: 1, paddingHorizontal: 10, paddingVertical: 7 },
  chipSelected: { backgroundColor: "#eee8ff", borderColor: "#7047d9" },
  chipText: { color: "#625b70", fontSize: 13 },
  chipTextSelected: { color: "#5732af", fontWeight: "700" },
  switchRow: { alignItems: "center", flexDirection: "row", flexWrap: "wrap", gap: 8 },
  switchLabel: { color: "#4d475d", fontSize: 14, fontWeight: "700" },
  input: { backgroundColor: "#fbfaff", borderColor: "#ded9e8", borderRadius: 12, borderWidth: 1, color: "#292540", fontSize: 15, padding: 12 },
  messageInput: { minHeight: 76, textAlignVertical: "top" },
  themeInput: { minHeight: 80, textAlignVertical: "top" },
  primaryButton: { alignItems: "center", backgroundColor: "#6a3bd0", borderRadius: 12, minHeight: 48, justifyContent: "center", paddingHorizontal: 16 },
  primaryButtonText: { color: "#fff", fontSize: 15, fontWeight: "700" },
  secondaryButton: { alignItems: "center", borderColor: "#d8cfef", borderRadius: 12, borderWidth: 1, minHeight: 46, justifyContent: "center", paddingHorizontal: 16 },
  secondaryButtonText: { color: "#5934aa", fontSize: 15, fontWeight: "700" },
  buttonDisabled: { opacity: 0.58 },
  refreshButton: { paddingHorizontal: 8, paddingVertical: 5 },
  refreshText: { color: "#6038ba", fontSize: 14, fontWeight: "700" },
  jobCard: { borderTopColor: "#edeaf2", borderTopWidth: 1, gap: 7, paddingTop: 14 },
  jobTitle: { color: "#302b45", fontSize: 16, fontWeight: "700" },
  jobDetail: { color: "#716a80", fontSize: 13, lineHeight: 19 },
  error: { color: "#b02f42", fontSize: 13, lineHeight: 19 },
  audioButton: { alignSelf: "flex-start", backgroundColor: "#f0edfb", borderRadius: 9, paddingHorizontal: 11, paddingVertical: 8 },
  audioButtonText: { color: "#5833ad", fontSize: 13, fontWeight: "700" },
});
