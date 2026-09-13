(() => {
  "use strict";

  const $v = (selector) => document.querySelector(selector);
  const vt = (zh, en) => (typeof isEnglish === "function" && isEnglish() ? en : zh);
  const defaults = {
    voice_language: "auto",
    system_voice_language: "",
    voice_name: "",
    voice_rate: 1,
    voice_auto_speak: true,
    voice_hands_free: true,
    voice_auto_continue: true,
  };
  let preferences = { ...defaults };
  let recognition = null;
  let recognitionWanted = false;
  let recognitionStarting = false;
  let recognitionGeneration = 0;
  let restartTimer = null;
  let autoSendTimer = null;
  let stream = null;
  let audioContext = null;
  let meterFrame = null;
  let speechQueue = [];
  let speaking = false;
  let serverAudio = null;
  let serverAudioUrl = "";
  let speechAbortController = null;
  let speakerStartedAt = 0;
  let bargeFrames = 0;
  let level = 0;
  let awaitingTask = false;
  let activeVoiceTaskId = "";
  let spokenTaskId = "";
  let preferencesReady = Promise.resolve();
  let liveInterimText = "";
  let draftAutoFollow = true;
  const AUTO_SEND_SILENCE_MS = 2400;
  const DRAFT_BOTTOM_THRESHOLD = 18;
  let autoSendGeneration = 0;

  function setState(state, title, detail = "") {
    const orb = $v("#voiceOrb");
    if (orb) orb.dataset.state = state;
    if ($v("#voiceStatus")) $v("#voiceStatus").textContent = title;
    if (detail && $v("#voiceStatusDetail")) $v("#voiceStatusDetail").textContent = detail;
    $v("#voiceMicHealth")?.setAttribute("data-active", String(state === "listening"));
    $v("#voiceAgentHealth")?.setAttribute("data-active", String(state === "thinking"));
    $v("#voiceSpeakerHealth")?.setAttribute("data-active", String(state === "speaking"));
  }

  async function loadPreferences() {
    try {
      const current = await api("/api/settings");
      preferences = { ...defaults, ...current };
    } catch {
      preferences = { ...defaults };
    }
  }

  function recognitionLanguage() {
    if (preferences.voice_language && preferences.voice_language !== "auto") return preferences.voice_language;
    const systemLanguage = String(
      preferences.system_voice_language
      || navigator.languages?.[0]
      || navigator.language
      || "en-US",
    ).replace("_", "-");
    return systemLanguage;
  }

  function appendMessage(role, text) {
    const transcript = $v("#voiceTranscript");
    transcript.querySelector(".voice-transcript-empty")?.remove();
    const article = document.createElement("article");
    article.className = `voice-message ${role}`;
    const label = document.createElement("small");
    label.textContent = role === "user" ? vt("你", "You") : "Elren";
    const body = document.createElement("div");
    body.className = "markdown";
    if (typeof renderMarkdown === "function") body.innerHTML = renderMarkdown(String(text || ""));
    else body.textContent = String(text || "");
    article.append(label, body);
    transcript.append(article);
    transcript.scrollTop = transcript.scrollHeight;
  }

  function draftIsNearBottom(draft = $v("#voiceDraft")) {
    if (!draft) return true;
    return draft.scrollHeight - draft.scrollTop - draft.clientHeight <= DRAFT_BOTTOM_THRESHOLD;
  }

  function setVoiceDraft(value = "", { follow = true } = {}) {
    const draft = $v("#voiceDraft");
    if (!draft) return;
    liveInterimText = "";
    draft.value = String(value || "");
    draftAutoFollow = follow;
    if (follow) draft.scrollTop = draft.scrollHeight;
  }

  function updateLiveDraft(finalText = "", interimText = "") {
    const draft = $v("#voiceDraft");
    if (!draft) return;
    const savedScrollTop = draft.scrollTop;
    const shouldFollow = draftAutoFollow || draftIsNearBottom(draft);
    let committed = draft.value;
    if (liveInterimText && committed.endsWith(liveInterimText)) {
      committed = committed.slice(0, -liveInterimText.length).trimEnd();
    }
    const finalPart = String(finalText || "").trim();
    if (finalPart) committed = `${committed}${committed.trim() ? " " : ""}${finalPart}`;
    liveInterimText = String(interimText || "").trim();
    draft.value = `${committed}${committed.trim() && liveInterimText ? " " : ""}${liveInterimText}`;
    if (shouldFollow) {
      draft.scrollTop = draft.scrollHeight;
      draftAutoFollow = true;
    } else {
      draft.scrollTop = savedScrollTop;
    }
  }

  function cancelAutoSend() {
    autoSendGeneration += 1;
    clearTimeout(autoSendTimer);
    autoSendTimer = null;
  }

  function scheduleAutoSend() {
    cancelAutoSend();
    if (!preferences.voice_hands_free || speaking) return;
    const generation = autoSendGeneration;
    autoSendTimer = setTimeout(() => {
      autoSendTimer = null;
      if (generation !== autoSendGeneration || speaking || !$v("#voiceDialog")?.open) return;
      submitTurn();
    }, AUTO_SEND_SILENCE_MS);
  }

  function resetVoiceSession({ keepListening = false } = {}) {
    const shouldResume = keepListening && Boolean($v("#voiceDialog")?.open);
    cancelAutoSend();
    stopRecognition();
    stopSpeech();
    awaitingTask = false;
    activeVoiceTaskId = "";
    spokenTaskId = "";
    const transcript = $v("#voiceTranscript");
    if (transcript) {
      transcript.replaceChildren();
      const empty = document.createElement("div");
      empty.className = "voice-transcript-empty";
      empty.textContent = vt("本次语音会话的转写与回答会显示在这里。", "Transcripts and answers from this voice session appear here.");
      transcript.append(empty);
    }
    setVoiceDraft("");
    if (shouldResume) setTimeout(resumeRecognition, 80);
  }

  function stopSpeech(interrupted = false) {
    speechQueue = [];
    speaking = false;
    bargeFrames = 0;
    speechAbortController?.abort();
    speechAbortController = null;
    try { serverAudio?.pause(); } catch {}
    try {
      serverAudio?.removeAttribute("src");
      serverAudio?.load();
    } catch {}
    serverAudio = null;
    if (serverAudioUrl) URL.revokeObjectURL(serverAudioUrl);
    serverAudioUrl = "";
    try { window.speechSynthesis?.cancel(); } catch {}
    if (interrupted) setState("listening", vt("已打断，正在听你说", "Interrupted — listening"), vt("继续说话即可改变方向。", "Speak now to change direction."));
  }

  function finishSpeechQueue() {
    speaking = false;
    $v("#voiceSpeakerHealth")?.setAttribute("data-active", "false");
    if ($v("#voiceDialog")?.open && preferences.voice_auto_continue) resumeRecognition();
    else if ($v("#voiceDialog")?.open) setState("idle", vt("回复完成", "Response complete"), vt("点击麦克风开始下一轮。", "Select the microphone for another turn."));
  }

  function playbackError(error) {
    speechQueue = [];
    speaking = false;
    const detail = String(error?.message || error || vt("语音播放失败", "Speech playback failed"));
    setState("error", vt("朗读未完成", "Speech playback stopped"), detail);
    if (typeof showToast === "function") showToast(`${vt("语音播放失败：", "Speech playback failed: ")}${detail}`, "error");
  }

  function speechRequest(chunk) {
    return {
      text: chunk,
      language: preferences.voice_language || "auto",
      voice_name: preferences.voice_name || "",
      rate: Number(preferences.voice_rate || 1),
    };
  }

  function bindServerAudio(audio, { revokeObjectUrl = false } = {}) {
    let started = false;
    serverAudio = audio;
    audio.onplay = () => {
      started = true;
      speaking = true;
      speakerStartedAt = performance.now();
      setState("speaking", vt("正在朗读回复", "Reading the reply"), vt("可以随时停止或开始下一轮。", "You can stop playback or begin another turn at any time."));
    };
    audio.onended = () => {
      serverAudio = null;
      if (revokeObjectUrl && serverAudioUrl) URL.revokeObjectURL(serverAudioUrl);
      serverAudioUrl = "";
      speakNext();
    };
    audio.onerror = () => {
      if (started) playbackError(vt("音频解码或输出设备不可用。", "Audio decoding or the output device is unavailable."));
    };
    return audio;
  }

  async function playCompletedServerAudio(chunk, controller) {
    const response = await fetch("/api/speech/synthesize", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(speechRequest(chunk)),
      signal: controller.signal,
    });
    if (!response.ok) throw new Error((await response.text()).slice(0, 300) || `HTTP ${response.status}`);
    const blob = await response.blob();
    if (controller.signal.aborted) return;
    if (serverAudioUrl) URL.revokeObjectURL(serverAudioUrl);
    serverAudioUrl = URL.createObjectURL(blob);
    const audio = bindServerAudio(new Audio(serverAudioUrl), { revokeObjectUrl: true });
    await audio.play();
  }

  async function speakNextFromServer() {
    const chunk = speechQueue.shift();
    const controller = new AbortController();
    speechAbortController = controller;
    setState("thinking", vt("正在准备语音回复", "Preparing the spoken reply"), vt("神经语音将在首批音频到达后立即播放。", "Neural playback starts as soon as the first audio arrives."));
    try {
      try {
        const sessionResponse = await fetch("/api/speech/stream-sessions", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(speechRequest(chunk)),
          signal: controller.signal,
        });
        if (!sessionResponse.ok) throw new Error((await sessionResponse.text()).slice(0, 300) || `HTTP ${sessionResponse.status}`);
        const session = await sessionResponse.json();
        if (controller.signal.aborted || !session.stream_url) return;
        const audio = bindServerAudio(new Audio(session.stream_url));
        audio.preload = "auto";
        await audio.play();
      } catch (streamError) {
        if (streamError?.name === "AbortError" || controller.signal.aborted) return;
        // Older packages or a temporarily unavailable streaming engine still
        // get the proven complete-file path instead of losing speech entirely.
        await playCompletedServerAudio(chunk, controller);
      }
    } catch (error) {
      if (error?.name !== "AbortError") playbackError(error);
    } finally {
      if (speechAbortController === controller) speechAbortController = null;
    }
  }

  function speechChunks(text) {
    let rest = String(text || "")
      .replace(/```[\s\S]*?```/g, vt(" 代码内容 ", " code block "))
      .replace(/[`*_#>|]/g, " ").replace(/\s+/g, " ").trim().slice(0, 12000);
    const chunks = [];
    while (rest) {
      let end = Math.min(900, rest.length);
      if (end < rest.length) {
        const boundary = Math.max(rest.lastIndexOf("。", end), rest.lastIndexOf("！", end), rest.lastIndexOf("？", end), rest.lastIndexOf(". ", end));
        if (boundary > 300) end = boundary + 1;
      }
      chunks.push(rest.slice(0, end));
      rest = rest.slice(end).trim();
    }
    return chunks;
  }

  function speakNext() {
    if (!speechQueue.length) {
      finishSpeechQueue();
      return;
    }
    const selectedVoice = String(preferences.voice_name || "");
    const explicitLocalVoice = Boolean(selectedVoice) && !selectedVoice.endsWith("Neural");
    if (!explicitLocalVoice || !window.speechSynthesis || typeof SpeechSynthesisUtterance !== "function") {
      speakNextFromServer();
      return;
    }
    const chunk = speechQueue.shift();
    const utterance = new SpeechSynthesisUtterance(chunk);
    const voices = window.speechSynthesis?.getVoices?.() || [];
    const chosen = voices.find((voice) => voice.name === preferences.voice_name)
      || voices.find((voice) => voice.lang.toLowerCase().startsWith(recognitionLanguage().slice(0, 2).toLowerCase()));
    if (chosen) utterance.voice = chosen;
    utterance.lang = chosen?.lang || recognitionLanguage();
    utterance.rate = Number(preferences.voice_rate || 1);
    utterance.onstart = () => {
      speaking = true;
      speakerStartedAt = performance.now();
      setState("speaking", vt("正在回复 · 可随时打断", "Speaking · interrupt at any time"), vt("点击“打断回复”，或直接开始说话。", "Select Interrupt, or simply start speaking."));
      if (preferences.voice_auto_continue) resumeRecognition();
    };
    utterance.onend = speakNext;
    utterance.onerror = () => {
      speaking = false;
      speechQueue.unshift(chunk);
      setState("thinking", vt("正在切换自然语音", "Switching to neural speech"), vt("本机声音未能播放，正在自动恢复。", "The local voice could not play; recovering automatically."));
      speakNextFromServer();
    };
    window.speechSynthesis.speak(utterance);
  }

  async function speakAnswer(task) {
    const displayResult = task?.display_result ?? task?.result;
    if (!$v("#voiceDialog")?.open || task?.status !== "completed" || !displayResult || spokenTaskId === task.id) return;
    if (activeVoiceTaskId && task.id !== activeVoiceTaskId) return;
    spokenTaskId = task.id;
    await preferencesReady;
    awaitingTask = false;
    if ($v("#voiceDialog")?.open) {
      activeVoiceTaskId = task.id;
      appendMessage("assistant", displayResult);
    }
    if (!preferences.voice_auto_speak) {
      setState("idle", vt("回复完成", "Response complete"), vt("最终文字已显示。", "The final answer is shown above."));
      if ($v("#voiceDialog")?.open && preferences.voice_auto_continue) resumeRecognition();
      return;
    }
    stopSpeech();
    speechQueue = speechChunks(displayResult);
    speakNext();
  }

  function stopRecognition({ keepWanted = false } = {}) {
    recognitionGeneration += 1;
    recognitionStarting = false;
    clearTimeout(restartTimer);
    restartTimer = null;
    if (!keepWanted) recognitionWanted = false;
    const current = recognition;
    recognition = null;
    try { current?.stop(); } catch {}
    stopMeter();
    $v("#voiceMicToggle")?.setAttribute("aria-pressed", "false");
    const label = $v("#voiceMicToggle span:last-child");
    if (label) label.textContent = vt("开始聆听", "Start listening");
  }

  async function startMeter() {
    if (stream || !navigator.mediaDevices?.getUserMedia) return;
    const generation = recognitionGeneration;
    try {
      const captured = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } });
      if (generation !== recognitionGeneration || !recognitionWanted || !$v("#voiceDialog")?.open) {
        captured.getTracks().forEach((track) => track.stop());
        return;
      }
      stream = captured;
      audioContext = new (window.AudioContext || window.webkitAudioContext)();
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 256;
      audioContext.createMediaStreamSource(stream).connect(analyser);
      const values = new Uint8Array(analyser.frequencyBinCount);
      const tick = () => {
        analyser.getByteFrequencyData(values);
        const raw = values.reduce((sum, value) => sum + value, 0) / Math.max(1, values.length) / 255;
        level += (raw - level) * 0.25;
        $v("#voiceOrb")?.style.setProperty("--voice-level", String(Math.min(1, level * 4.5)));
        if (speaking && performance.now() - speakerStartedAt > 900 && level > 0.5) bargeFrames += 1;
        else bargeFrames = Math.max(0, bargeFrames - 1);
        if (speaking && bargeFrames >= 8) stopSpeech(true);
        meterFrame = requestAnimationFrame(tick);
      };
      tick();
    } catch {
      // Dictation remains available even when the visual meter cannot open a parallel stream.
      if (generation === recognitionGeneration) stopMeter();
    }
  }

  function stopMeter() {
    if (meterFrame) cancelAnimationFrame(meterFrame);
    meterFrame = null;
    stream?.getTracks().forEach((track) => track.stop());
    stream = null;
    try { audioContext?.close(); } catch {}
    audioContext = null;
    level = 0;
  }

  function recognitionError(error) {
    const known = {
      "not-allowed": vt("听写服务未获授权或不可用；麦克风权限与语音识别权限可能不同。", "Dictation is not authorized or available; microphone and speech-recognition permissions may differ."),
      "service-not-allowed": vt("系统语音识别服务不可用，请检查系统的听写或语音识别设置。", "The system speech service is unavailable. Check the system dictation or speech-recognition settings."),
      "audio-capture": vt("没有检测到可用麦克风。", "No working microphone was detected."),
      network: vt("语音服务暂时断开，正在自动恢复。", "The speech service disconnected and is recovering."),
      "no-speech": vt("没有听到语音，继续等待。", "No speech detected; still listening."),
      aborted: vt("聆听已暂停。", "Listening paused."),
    };
    return known[error] || `${vt("语音识别未完成：", "Speech recognition did not complete: ")}${error}`;
  }

  function microphoneError(error) {
    const name = String(error?.name || "");
    if (["NotFoundError", "DevicesNotFoundError"].includes(name)) {
      return vt("未检测到麦克风，请连接麦克风后重试。", "No microphone detected. Connect a microphone and try again.");
    }
    if (["NotReadableError", "TrackStartError", "AbortError"].includes(name)) {
      return vt("无法读取麦克风，设备可能被占用、断开或发生故障。", "The microphone could not be read. It may be busy, disconnected, or unavailable.");
    }
    if (["NotAllowedError", "PermissionDeniedError", "SecurityError"].includes(name)) {
      return vt("无法访问麦克风，请检查应用和系统的麦克风访问权限。", "Microphone access is blocked. Check microphone access for the app and system.");
    }
    return vt("麦克风暂时不可用，请检查输入设备后重试。", "The microphone is unavailable. Check your input device and try again.");
  }

  async function resumeRecognition() {
    if (!$v("#voiceDialog")?.open || recognition || recognitionStarting) return;
    const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Recognition) {
      recognitionWanted = false;
      setState("error", vt("当前系统不支持实时听写", "Realtime dictation is unavailable"), vt("仍可在下方输入文字并使用语音朗读。", "You can still type below and use spoken answers."));
      return;
    }
    recognitionWanted = true;
    recognitionStarting = true;
    const generation = ++recognitionGeneration;
    // SpeechRecognition can collapse a missing device into "not-allowed" on
    // WebKit. Check capture first so hardware and permission errors stay distinct.
    // The short probe stream is always closed, including after a cancelled start.
    try {
      if (navigator.mediaDevices?.getUserMedia) {
        const probe = await navigator.mediaDevices.getUserMedia({ audio: true });
        probe.getTracks().forEach((track) => track.stop());
      }
    } catch (error) {
      if (generation !== recognitionGeneration) return;
      recognitionStarting = false;
      recognitionWanted = false;
      setState("error", microphoneError(error), vt("可以连接设备后点击“开始聆听”，或使用文字输入继续。", "Connect a device and select Start listening, or continue with text input."));
      return;
    }
    if (generation !== recognitionGeneration || !recognitionWanted || !$v("#voiceDialog")?.open) return;
    recognitionStarting = false;
    let instance;
    try { instance = new Recognition(); }
    catch (error) {
      stopRecognition();
      setState("error", recognitionError(error?.name === "NotAllowedError" ? "not-allowed" : error?.name || "unavailable"), vt("请检查系统听写服务后重试，或使用文字输入。", "Check system dictation and try again, or use text input."));
      return;
    }
    recognition = instance;
    instance.lang = recognitionLanguage();
    $v("#voiceDialog")?.setAttribute("data-recognition-language", instance.lang);
    instance.interimResults = true;
    instance.continuous = true;
    instance.maxAlternatives = 1;
    instance.onstart = () => {
      if (recognition !== instance) return;
      $v("#voiceMicToggle")?.setAttribute("aria-pressed", "true");
      const label = $v("#voiceMicToggle span:last-child");
      if (label) label.textContent = vt("暂停聆听", "Pause listening");
      if (!speaking) setState("listening", vt("正在聆听", "Listening"), vt("说完可自动发送，也可以先修改转写文字。", "Your turn can be sent automatically, or edited first."));
      startMeter();
    };
    instance.onspeechstart = () => {
      if (recognition !== instance) return;
      cancelAutoSend();
      if (speaking && performance.now() - speakerStartedAt > 700) stopSpeech(true);
    };
    instance.onresult = (event) => {
      if (recognition !== instance) return;
      let finalText = "";
      let interimText = "";
      for (let index = event.resultIndex; index < event.results.length; index += 1) {
        const text = event.results[index][0]?.transcript || "";
        if (event.results[index].isFinal) finalText += text;
        else interimText += text;
      }
      if (interimText.trim()) cancelAutoSend();
      updateLiveDraft(finalText, interimText);
      if (finalText.trim()) scheduleAutoSend();
    };
    instance.onerror = (event) => {
      if (recognition !== instance) return;
      const recoverable = ["no-speech", "network", "aborted"].includes(event.error);
      setState(recoverable ? "idle" : "error", recognitionError(event.error), recoverable ? vt("保持语音窗口开启时会自动恢复。", "Listening recovers automatically while this window remains open.") : vt("可以使用文字输入继续。", "You can continue with text input."));
      if (!recoverable) stopRecognition();
    };
    instance.onend = () => {
      if (recognition !== instance) return;
      recognition = null;
      recognitionGeneration += 1;
      stopMeter();
      if (recognitionWanted && $v("#voiceDialog")?.open) restartTimer = setTimeout(resumeRecognition, 350);
    };
    try { instance.start(); }
    catch (error) {
      // Persistent start failures must not create a silent, unbounded retry loop.
      stopRecognition();
      setState("error", recognitionError(error?.name === "NotAllowedError" ? "not-allowed" : error?.name || "unavailable"), vt("请检查系统听写服务后重试，或使用文字输入。", "Check system dictation and try again, or use text input."));
    }
  }

  async function submitTurn() {
    cancelAutoSend();
    const draft = $v("#voiceDraft");
    const text = draft.value.trim();
    if (!text) return;
    if (startRequestPending) {
      setState("thinking", vt("正在发送上一条要求", "The previous turn is still being sent"), vt("请等待发送完成后再试。", "Wait for it to finish sending, then try again."));
      return;
    }
    stopSpeech();
    stopRecognition();
    appendMessage("user", text);
    setVoiceDraft("");
    $v("#prompt").value = text;
    resizePromptInput();
    awaitingTask = true;
    activeVoiceTaskId = "";
    setState("thinking", vt("Agent 正在思考和使用工具", "Agent is thinking and using tools"), vt("本轮继续使用当前聊天上下文与全部工具。", "This turn keeps the current chat context and all tools."));
    try {
      const sent = await start({ voiceRequest: true });
      if (!sent) {
        awaitingTask = false;
        setVoiceDraft(text);
        setState("error", vt("本轮未能发送", "This turn could not be sent"), vt("请检查连接后重试。", "Check the connection and try again."));
        return;
      }
      activeVoiceTaskId = taskId || currentTaskSnapshot?.id || "";
    } catch (error) {
      awaitingTask = false;
      setVoiceDraft(text);
      setState("error", vt("本轮未能发送", "This turn could not be sent"), String(error?.message || error));
    }
  }

  function interrupt() {
    stopSpeech(true);
    if (currentTaskSnapshot?.status === "running" && typeof requestStop === "function") {
      void requestStop();
    }
    awaitingTask = false;
    resumeRecognition();
  }

  function toggleMic() {
    if (recognitionWanted || recognition) {
      stopRecognition();
      setState("idle", vt("麦克风已静音", "Microphone muted"), vt("转写草稿已保留。", "Your transcript draft is preserved."));
    } else {
      if (speaking) stopSpeech(true);
      resumeRecognition();
    }
  }

  async function openVoice() {
    preferencesReady = loadPreferences();
    await preferencesReady;
    const dialog = $v("#voiceDialog");
    if (!dialog.open) dialog.showModal();
    $v("#voiceConversation")?.setAttribute("aria-pressed", "true");
    setState("idle", vt("准备开始", "Ready"), vt("点击麦克风开始连续对话，也可以直接编辑文字后发送。", "Start continuous conversation with the microphone, or type and send."));
    setTimeout(resumeRecognition, 80);
  }

  function closeVoice() {
    resetVoiceSession();
    stopMeter();
    $v("#voiceDialog")?.close();
    $v("#voiceConversation")?.setAttribute("aria-pressed", "false");
  }

  function handleTask(task) {
    const dialogOpen = Boolean($v("#voiceDialog")?.open);
    if (!dialogOpen || (!awaitingTask && !activeVoiceTaskId)) return;
    if (dialogOpen && awaitingTask && !activeVoiceTaskId) activeVoiceTaskId = task?.id || "";
    if (dialogOpen && activeVoiceTaskId && task?.id !== activeVoiceTaskId) return;
    if (["queued", "running", "waiting_approval", "waiting_user", "waiting_human"].includes(task?.status)) {
      const waitingForUser = ["waiting_user", "waiting_human"].includes(task.status);
      const waitingForApproval = task.status === "waiting_approval";
      const statusText = waitingForUser
        ? vt("需要你接管操作", "Your input is required")
        : waitingForApproval
          ? vt("Agent 正在确认操作", "Agent is confirming the action")
          : vt("Agent 正在思考和使用工具", "Agent is thinking and using tools");
      const detailText = waitingForUser
        ? vt("请在主聊天完成接管后继续。", "Complete the takeover in the main chat, then continue.")
        : waitingForApproval
          ? vt("确认完成后会自动继续。", "The task will continue automatically after confirmation.")
          : vt("可随时打断本轮。", "You can interrupt this turn at any time.");
      setState("thinking", statusText, detailText);
    } else if (task?.status === "completed") speakAnswer(task);
    else if (["failed", "stopped", "cancelled", "loop_paused"].includes(task?.status)) {
      awaitingTask = false;
      setState("error", vt("本轮已结束", "This turn ended"), String(task.error || task.stop_reason || vt("可以修改要求后重试。", "Edit the request and try again.")));
      if (preferences.voice_auto_continue) resumeRecognition();
    }
  }

  function translateVoiceUi() {
    if (!isEnglish()) return;
    const set = (selector, value) => { const node = $v(selector); if (node) node.textContent = value; };
    $v("#voiceConversation")?.setAttribute("title", "Voice conversation");
    $v("#voiceConversation")?.setAttribute("aria-label", "Open voice conversation");
    set("#voiceDialogTitle", "Live voice conversation");
    set("#voiceStatus", "Ready");
    set("#voiceStatusDetail", "Start continuous conversation with the microphone, or type and send.");
    set("#voiceMicHealth", "Microphone"); set("#voiceAgentHealth", "Agent"); set("#voiceSpeakerHealth", "Speaker");
    $v("#voiceDialog .voice-health")?.setAttribute("aria-label", "Voice channel status");
    $v("#voiceTranscript")?.setAttribute("aria-label", "Voice conversation transcript");
    $v("#voiceDraft")?.setAttribute("aria-label", "Voice transcription draft");
    set("#voiceTranscript .voice-transcript-empty", "Transcripts and answers from this voice session appear here.");
    set("#voiceMicToggle span:last-child", "Start listening");
    set("#voiceSend", "Send turn"); set("#voiceInterrupt", "Interrupt response");
    $v("#voiceDraft").placeholder = "Live dictation appears here first. Edit it before sending if needed…";
    $v("#voiceClose").setAttribute("aria-label", "Close voice conversation");
  }

  $v("#voiceConversation").onclick = openVoice;
  $v("#voiceClose").onclick = closeVoice;
  $v("#voiceMicToggle").onclick = toggleMic;
  $v("#voiceSend").onclick = submitTurn;
  $v("#voiceInterrupt").onclick = interrupt;
  $v("#voiceDraft").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); submitTurn(); }
  });
  $v("#voiceDraft").addEventListener("input", () => {
    cancelAutoSend();
    liveInterimText = "";
    draftAutoFollow = draftIsNearBottom();
  });
  $v("#voiceDraft").addEventListener("scroll", () => {
    draftAutoFollow = draftIsNearBottom();
  }, { passive: true });
  $v("#voiceDialog").addEventListener("cancel", (event) => { event.preventDefault(); closeVoice(); });
  $v("#voiceDialog").addEventListener("close", () => { resetVoiceSession(); stopMeter(); $v("#voiceConversation")?.setAttribute("aria-pressed", "false"); });
  window.addEventListener("elren:task-view-changed", () => resetVoiceSession({ keepListening: true }));
  window.addEventListener("elren:task-updated", (event) => handleTask(event.detail));
  window.addEventListener("elren:voice-settings-updated", (event) => {
    preferences = { ...defaults, ...(event.detail || {}) };
  });
  translateVoiceUi();
})();
