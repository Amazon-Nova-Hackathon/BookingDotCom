import { useState, useRef, useCallback, useEffect } from 'react';

type ConnectionStatus = 'idle' | 'connecting' | 'connected';
const BROWSER_VIEWPORT_WIDTH = 1280;
const BROWSER_VIEWPORT_HEIGHT = 800;

interface ChatMessage {
    role: 'user' | 'bot';
    text: string;
    kind?: 'speech' | 'status';
}

export default function App() {
    const [status, setStatus] = useState<ConnectionStatus>('idle');
    const [chatHistory, setChatHistory] = useState<ChatMessage[]>([]);
    const [isBrowserActive, setIsBrowserActive] = useState(false);

    const [isMuted, setIsMuted] = useState(false);
    const [hasBrowserFrame, setHasBrowserFrame] = useState(false);

    const pcRef = useRef<RTCPeerConnection | null>(null);
    const pcIdRef = useRef<string | null>(null);
    const mediaStreamRef = useRef<MediaStream | null>(null);
    const remoteAudioRef = useRef<HTMLAudioElement | null>(null);
    const chatEndRef = useRef<HTMLDivElement>(null);
    const eventSourceRef = useRef<EventSource | null>(null);
    const sseReconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const disconnectedRef = useRef(true);
    const browserWsReconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const browserWsRetryCount = useRef(0);
    const pendingToolStatusRef = useRef<string | null>(null);
    const startNewBotTurnRef = useRef(false);

    // Canvas + WebSocket refs for CDP screencast
    const canvasRef = useRef<HTMLCanvasElement>(null);
    const browserWsRef = useRef<WebSocket | null>(null);
    const imgDecoderRef = useRef(new Image());

    const getToolStatusMessage = useCallback((action: string) => {
        switch (action) {
            case 'search_hotel':
                return 'Understood. Processing your search now.';
            case 'select_hotel':
                return 'Understood. Opening that hotel now.';
            case 'reserve_hotel':
                return 'Understood. Starting the booking flow now.';
            case 'fill_guest_info':
                return 'Understood. Filling the guest form now.';
            case 'continue_to_payment':
                return 'Understood. Moving to the next booking step now.';
            default:
                return 'Understood. Processing now.';
        }
    }, []);

    useEffect(() => {
        chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [chatHistory]);

    // ── Browser WebSocket (CDP screencast) ─────────────────────────────────
    const connectBrowserWs = useCallback(() => {
        if (browserWsRef.current?.readyState === WebSocket.OPEN) return;

        const proto = location.protocol === 'https:' ? 'wss' : 'ws';
        const wsHost = location.hostname + ':7863';
        const ws = new WebSocket(`${proto}://${wsHost}/ws/browser`);
        browserWsRef.current = ws;

        ws.onopen = () => {
            console.log('[BrowserWS] Connected');
            setIsBrowserActive(true);
        };

        ws.onmessage = (e) => {
            try {
                const msg = JSON.parse(e.data);
                if (msg.type === 'frame' && msg.data) {
                    const img = imgDecoderRef.current;
                    img.onload = () => {
                        const canvas = canvasRef.current;
                        if (canvas) {
                            const ctx = canvas.getContext('2d');
                            if (ctx) {
                                ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
                                if (!hasBrowserFrame) setHasBrowserFrame(true);
                            }
                        }
                    };
                    img.src = `data:image/jpeg;base64,${msg.data}`;
                }
            } catch { /* ignore parse errors */ }
        };

        ws.onclose = () => {
            console.log('[BrowserWS] Disconnected');
            browserWsRef.current = null;
            if (isBrowserActive && !disconnectedRef.current) {
                const delay = Math.min(1000 * Math.pow(2, browserWsRetryCount.current), 5000);
                browserWsRetryCount.current++;
                console.log(`[BrowserWS] Reconnecting in ${delay}ms (attempt ${browserWsRetryCount.current})`);
                browserWsReconnectTimer.current = setTimeout(connectBrowserWs, delay);
            }
        };

        ws.onerror = () => {
            ws.close();
        };
    }, [hasBrowserFrame, isBrowserActive]);

    const disconnectBrowserWs = useCallback(() => {
        if (browserWsReconnectTimer.current) {
            clearTimeout(browserWsReconnectTimer.current);
            browserWsReconnectTimer.current = null;
        }
        browserWsRetryCount.current = 0;
        if (browserWsRef.current) {
            browserWsRef.current.close();
            browserWsRef.current = null;
        }
        setHasBrowserFrame(false);
    }, []);

    // ── Send input events to browser via WebSocket ─────────────────────────
    const sendBrowserInput = useCallback((payload: Record<string, unknown>) => {
        const ws = browserWsRef.current;
        if (ws?.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(payload));
        }
    }, []);

    const scaleCoords = useCallback((clientX: number, clientY: number) => {
        const canvas = canvasRef.current;
        if (!canvas) return { x: 0, y: 0 };
        const rect = canvas.getBoundingClientRect();
        return {
            x: Math.round((clientX - rect.left) / rect.width * BROWSER_VIEWPORT_WIDTH),
            y: Math.round((clientY - rect.top) / rect.height * BROWSER_VIEWPORT_HEIGHT),
        };
    }, []);

    const handleCanvasClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
        const { x, y } = scaleCoords(e.clientX, e.clientY);
        console.log(`[Click] (${x}, ${y})`);
        sendBrowserInput({ type: 'click', x, y });
    }, [scaleCoords, sendBrowserInput]);

    const handleCanvasKeyDown = useCallback((e: React.KeyboardEvent<HTMLCanvasElement>) => {
        const specialKeys = ['Enter', 'Tab', 'Escape', 'Backspace', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Delete', 'Home', 'End', 'PageUp', 'PageDown'];
        if (specialKeys.includes(e.key)) {
            e.preventDefault();
            sendBrowserInput({ type: 'keypress', key: e.key });
        } else if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
            e.preventDefault();
            sendBrowserInput({ type: 'type', text: e.key });
        }
    }, [sendBrowserInput]);

    useEffect(() => {
        const canvas = canvasRef.current;
        if (!canvas) return;

        const handleWheel = (event: WheelEvent) => {
            event.preventDefault();
            const { x, y } = scaleCoords(event.clientX, event.clientY);
            sendBrowserInput({
                type: 'scroll',
                x,
                y,
                deltaX: Math.round(event.deltaX),
                deltaY: Math.round(event.deltaY),
            });
        };

        canvas.addEventListener('wheel', handleWheel, { passive: false });
        return () => {
            canvas.removeEventListener('wheel', handleWheel);
        };
    }, [hasBrowserFrame, isBrowserActive, scaleCoords, sendBrowserInput]);

    // ── SSE ───────────────────────────────────────────────────────────────
    const connectSSE = useCallback(() => {
        if (eventSourceRef.current) return;
        if (disconnectedRef.current) return;
        console.log('[SSE] Connecting...');
        const es = new EventSource('/api/events');
        eventSourceRef.current = es;

        es.onopen = () => console.log('[SSE] Connected');

        es.onmessage = (evt) => {
            try {
                const data = JSON.parse(evt.data);
                switch (data.type) {
                    case 'user_transcript':
                        setChatHistory(h => {
                            const nextHistory = [...h];
                            const trailingStatus = nextHistory[nextHistory.length - 1];
                            if (trailingStatus?.role === 'bot' && trailingStatus.kind === 'status') {
                                nextHistory.splice(nextHistory.length - 1, 0, { role: 'user', text: data.text });
                            } else {
                                nextHistory.push({ role: 'user', text: data.text });
                            }
                            if (pendingToolStatusRef.current) {
                                nextHistory.push({ role: 'bot', text: pendingToolStatusRef.current, kind: 'status' });
                                pendingToolStatusRef.current = null;
                            }
                            return nextHistory;
                        });
                        break;
                    case 'bot_response_start':
                        startNewBotTurnRef.current = true;
                        break;
                    case 'bot_response':
                        setChatHistory(h => {
                            const nextHistory = [...h];
                            const incoming = String(data.text || '');
                            const normalizedIncoming = incoming.trim().replace(/\s+/g, ' ');
                            const last = nextHistory[nextHistory.length - 1];

                            if (
                                pendingToolStatusRef.current &&
                                (last?.role === 'user' || !last || (last.role === 'bot' && last.kind === 'status'))
                            ) {
                                nextHistory.push({ role: 'bot', text: pendingToolStatusRef.current, kind: 'status' });
                                pendingToolStatusRef.current = null;
                            }

                            const mergeTarget = nextHistory[nextHistory.length - 1];
                            const normalizedLast = String(mergeTarget?.text || '').trim().replace(/\s+/g, ' ');
                            if (
                                !startNewBotTurnRef.current &&
                                mergeTarget?.role === 'bot' &&
                                mergeTarget.kind !== 'status' &&
                                normalizedIncoming.length > 18 &&
                                normalizedLast.includes(normalizedIncoming)
                            ) {
                                return nextHistory;
                            }

                            if (!startNewBotTurnRef.current && mergeTarget?.role === 'bot' && mergeTarget.kind !== 'status') {
                                return [
                                    ...nextHistory.slice(0, -1),
                                    { role: 'bot', text: mergeTarget.text + incoming, kind: 'speech' },
                                ];
                            }
                            startNewBotTurnRef.current = false;
                            return [...nextHistory, { role: 'bot', text: incoming, kind: 'speech' }];
                        });
                        break;
                    case 'tool_called':
                        setIsBrowserActive(true);
                        connectBrowserWs();
                        setChatHistory(h => {
                            const statusMessage = getToolStatusMessage(data.action);
                            const last = h[h.length - 1];
                            if (last?.role === 'user') {
                                return [...h, { role: 'bot', text: statusMessage, kind: 'status' }];
                            }
                            pendingToolStatusRef.current = statusMessage;
                            return h;
                        });
                        break;
                    case 'tool_result':
                        break;
                }
            } catch { }
        };

        es.onerror = () => {
            es.close();
            eventSourceRef.current = null;
            if (!disconnectedRef.current) {
                sseReconnectTimer.current = setTimeout(connectSSE, 2000);
            }
        };
    }, [connectBrowserWs, getToolStatusMessage]);

    useEffect(() => {
        return () => {
            eventSourceRef.current?.close();
            disconnectBrowserWs();
            if (sseReconnectTimer.current) clearTimeout(sseReconnectTimer.current);
        };
    }, [disconnectBrowserWs]);

    // ── WebRTC ────────────────────────────────────────────────────────────
    const sendIceCandidates = useCallback(async (candidates: RTCIceCandidate[], pcId: string) => {
        if (!candidates.length) return;
        await fetch('/offer', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                pc_id: pcId,
                candidates: candidates.map(c => ({
                    candidate: c.candidate,
                    sdpMid: c.sdpMid ?? '0',
                    sdpMLineIndex: c.sdpMLineIndex ?? 0,
                })),
            }),
        }).catch(console.warn);
    }, []);

    // ── Disconnect ────────────────────────────────────────────────────────
    const disconnect = useCallback(() => {
        disconnectedRef.current = true;

        mediaStreamRef.current?.getTracks().forEach(t => t.stop());
        mediaStreamRef.current = null;

        if (remoteAudioRef.current) {
            remoteAudioRef.current.pause();
            remoteAudioRef.current.srcObject = null;
            remoteAudioRef.current = null;
        }

        const pc = pcRef.current;
        if (pc) {
            pc.ontrack = null;
            pc.onicecandidate = null;
            pc.onconnectionstatechange = null;
            pc.close();
        }
        pcRef.current = null;
        pcIdRef.current = null;

        if (eventSourceRef.current) {
            eventSourceRef.current.close();
            eventSourceRef.current = null;
        }
        if (sseReconnectTimer.current) {
            clearTimeout(sseReconnectTimer.current);
            sseReconnectTimer.current = null;
        }
        pendingToolStatusRef.current = null;
        startNewBotTurnRef.current = false;

        setStatus('idle');
        setIsMuted(false);
        setIsBrowserActive(false);
    }, []);

    // ── Start ─────────────────────────────────────────────────────────────
    const startInteraction = async () => {
        try {
            disconnectedRef.current = false;
            setStatus('connecting');
            setChatHistory([]);
            pendingToolStatusRef.current = null;
            startNewBotTurnRef.current = false;
            setIsBrowserActive(true);

            setIsMuted(false);
            setHasBrowserFrame(false);

            connectBrowserWs();
            connectSSE();

            const stream = await navigator.mediaDevices.getUserMedia({
                audio: {
                    echoCancellation: true,
                    noiseSuppression: true,
                    autoGainControl: true,
                    channelCount: 1,
                },
            });
            mediaStreamRef.current = stream;
            const pc = new RTCPeerConnection({ iceServers: [{ urls: 'stun:stun.l.google.com:19302' }] });
            pcRef.current = pc;

            const pending: RTCIceCandidate[] = [];
            pc.onicecandidate = async (e) => {
                if (!e.candidate) return;
                if (pcIdRef.current) await sendIceCandidates([e.candidate], pcIdRef.current);
                else pending.push(e.candidate);
            };

            pc.ontrack = (e) => {
                if (remoteAudioRef.current) {
                    remoteAudioRef.current.pause();
                    remoteAudioRef.current.srcObject = null;
                }
                const audio = new Audio();
                audio.srcObject = e.streams[0];
                audio.play().catch(console.error);
                remoteAudioRef.current = audio;
            };

            pc.onconnectionstatechange = () => {
                const state = pc.connectionState;
                if (state === 'connected') setStatus('connected');
                else if (state === 'failed') disconnect();
            };

            stream.getTracks().forEach(t => pc.addTrack(t, stream));

            const offer = await pc.createOffer();
            await pc.setLocalDescription(offer);

            const res = await fetch('/offer', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ sdp: offer.sdp, type: offer.type }),
            });
            if (!res.ok) throw new Error(`Server error ${res.status}`);

            const answer = await res.json();
            pcIdRef.current = answer.pc_id;
            await pc.setRemoteDescription(new RTCSessionDescription({ type: 'answer', sdp: answer.sdp }));

            if (pending.length) {
                await sendIceCandidates(pending, answer.pc_id);
                pending.length = 0;
            }
        } catch (err) {
            console.error(err);
            setStatus('idle');
        }
    };

    // ── Mute toggle ───────────────────────────────────────────────────────
    const toggleMute = () => {
        const stream = mediaStreamRef.current;
        if (!stream) return;
        const audioTrack = stream.getAudioTracks()[0];
        if (!audioTrack) return;
        audioTrack.enabled = !audioTrack.enabled;
        setIsMuted(!audioTrack.enabled);
    };

    const lastUserMsg = [...chatHistory].reverse().find(m => m.role === 'user')?.text;
    const lastBotMsg = [...chatHistory].reverse().find(m => m.role === 'bot')?.text;

    return (
        <div style={{
            display: 'flex', flexDirection: 'column', height: '100vh',
            background: '#080816', color: '#e2e8f0',
            fontFamily: "'Inter', sans-serif", overflow: 'hidden',
        }}>

            {/* ── TOP NAV BAR ── */}
            <nav style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                padding: '0 24px', height: 52, minHeight: 52,
                background: 'rgba(12, 12, 28, 0.95)',
                borderBottom: '1px solid rgba(99, 102, 241, 0.12)',
                backdropFilter: 'blur(12px)',
            }}>
                <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                    <div style={{
                        width: 48, height: 48, borderRadius: 8,
                        background: 'transparent',
                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                        fontSize: 16,
                    }}><img src="/logo.png" alt="Logo" style={{ width: '100%', height: '100%', objectFit: 'cover', borderRadius: 8 }} /></div>
                    <div>
                        <span style={{
                            fontSize: 15, fontWeight: 700,
                            background: 'linear-gradient(135deg, #818cf8, #38bdf8)',
                            WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent',
                        }}>Booking Voice AI</span>
                        <span style={{ fontSize: 10, color: '#475569', marginLeft: 8, textTransform: 'uppercase', letterSpacing: 1.2 }}>Amazon Nova Hackathon</span>
                    </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
                    {status === 'connected' && (
                        <div style={{
                            display: 'flex', alignItems: 'center', gap: 6,
                            background: 'rgba(16, 185, 129, 0.12)',
                            border: '1px solid rgba(16, 185, 129, 0.3)',
                            borderRadius: 20, padding: '4px 12px',
                        }}>
                            <div style={{
                                width: 6, height: 6, borderRadius: '50%',
                                background: '#10b981',
                                boxShadow: '0 0 8px #10b981',
                                animation: 'pulse 2s ease-in-out infinite',
                            }} />
                            <span style={{ fontSize: 11, color: '#10b981', fontWeight: 600 }}>Live Session</span>
                        </div>
                    )}
                    {isBrowserActive && hasBrowserFrame && (
                        <div style={{
                            display: 'flex', alignItems: 'center', gap: 6,
                            background: 'rgba(56, 189, 248, 0.08)',
                            border: '1px solid rgba(56, 189, 248, 0.2)',
                            borderRadius: 20, padding: '4px 12px',
                        }}>
                            <span style={{ fontSize: 11, color: '#38bdf8', fontWeight: 500 }}>CDP Streaming</span>
                        </div>
                    )}
                </div>
            </nav>

            {/* ── MAIN CONTENT ── */}
            <div style={{ display: 'flex', flex: 1, overflow: 'hidden', minWidth: 0 }}>

                {/* ── LEFT SIDEBAR (28%) ── */}
                <div style={{
                    width: '28%', minWidth: 300, maxWidth: 380,
                    display: 'flex', flexDirection: 'column',
                    background: 'linear-gradient(180deg, #0c0c1e 0%, #0f0f1f 100%)',
                    borderRight: '1px solid rgba(99, 102, 241, 0.1)',
                }}>

                    {/* Voice Control Section */}
                    <div style={{
                        padding: '24px 20px',
                        display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 16,
                        borderBottom: '1px solid rgba(99, 102, 241, 0.08)',
                    }}>
                        <span style={{
                            fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
                            letterSpacing: 1.5, color: '#475569',
                        }}>Voice Control</span>

                        {/* Connection status */}
                        <div style={{
                            display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
                            color: status === 'connected'
                                ? (isMuted ? '#f59e0b' : '#10b981')
                                : status === 'connecting' ? '#f59e0b' : '#475569',
                        }}>
                            <div style={{
                                width: 7, height: 7, borderRadius: '50%',
                                background: status === 'connected'
                                    ? (isMuted ? '#f59e0b' : '#10b981')
                                    : status === 'connecting' ? '#f59e0b' : '#1e293b',
                                boxShadow: status === 'connected'
                                    ? `0 0 10px ${isMuted ? '#f59e0b' : '#10b981'}`
                                    : undefined,
                            }} />
                            <span style={{ fontWeight: 500 }}>
                                {status === 'connected'
                                    ? (isMuted ? 'Muted' : 'Connected — Start speaking')
                                    : status === 'connecting' ? 'Connecting...' : 'Ready'}
                            </span>
                        </div>

                        {/* Mic + Mute buttons */}
                        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                            <div style={{ position: 'relative', display: 'flex', justifyContent: 'center', alignItems: 'center' }}>
                                {status === 'connected' && (
                                    <div style={{
                                        position: 'absolute', width: 100, height: 100, borderRadius: '50%',
                                        background: 'rgba(239, 68, 68, 0.1)',
                                        animation: 'ping 2s ease-in-out infinite',
                                        pointerEvents: 'none',
                                    }} />
                                )}
                                <button
                                    id={status === 'idle' ? 'btn-start' : 'btn-stop'}
                                    onClick={status === 'idle' ? startInteraction : disconnect}
                                    style={{
                                        position: 'relative', zIndex: 1,
                                        width: 72, height: 72, borderRadius: '50%', border: 'none',
                                        cursor: status === 'connecting' ? 'wait' : 'pointer',
                                        display: 'flex', flexDirection: 'column', alignItems: 'center',
                                        justifyContent: 'center', gap: 2, fontSize: 10, fontWeight: 700,
                                        color: '#fff', transition: 'all 0.3s ease',
                                        background: status === 'connected'
                                            ? 'linear-gradient(135deg, #ef4444, #dc2626)'
                                            : status === 'connecting'
                                                ? 'linear-gradient(135deg, #f59e0b, #d97706)'
                                                : 'linear-gradient(135deg, #6366f1, #4f46e5)',
                                        boxShadow: status === 'connected'
                                            ? '0 0 32px rgba(239, 68, 68, 0.4)'
                                            : status === 'connecting'
                                                ? '0 0 32px rgba(245, 158, 11, 0.3)'
                                                : '0 0 32px rgba(99, 102, 241, 0.4)',
                                    }}
                                >
                                    <span style={{ fontSize: 22 }}>
                                        {status === 'connected' ? '⏹' : status === 'connecting' ? '⏳' : '🎙️'}
                                    </span>
                                    <span>{status === 'connected' ? 'Stop' : status === 'connecting' ? 'Wait...' : 'Start'}</span>
                                </button>
                            </div>

                            {status === 'connected' && (
                                <button
                                    onClick={toggleMute}
                                    title={isMuted ? 'Unmute' : 'Mute'}
                                    style={{
                                        width: 42, height: 42, borderRadius: '50%',
                                        cursor: 'pointer',
                                        background: isMuted
                                            ? 'linear-gradient(135deg, #ef4444, #dc2626)'
                                            : 'rgba(255, 255, 255, 0.06)',
                                        color: '#fff', fontSize: 18,
                                        display: 'flex', alignItems: 'center', justifyContent: 'center',
                                        border: isMuted ? 'none' : '1px solid rgba(255,255,255,0.1)',
                                        transition: 'all 0.3s',
                                    }}
                                >
                                    {isMuted ? '🔇' : '🎤'}
                                </button>
                            )}
                        </div>

                        {/* Audio waveform visualizer (decorative) */}
                        {status === 'connected' && !isMuted && (
                            <div style={{
                                display: 'flex', alignItems: 'center', justifyContent: 'center',
                                gap: 3, height: 24, width: '100%',
                                background: 'rgba(99, 102, 241, 0.06)',
                                borderRadius: 8, padding: '0 16px',
                            }}>
                                {Array.from({ length: 20 }).map((_, i) => (
                                    <div
                                        key={i}
                                        style={{
                                            width: 3, borderRadius: 2,
                                            background: 'linear-gradient(to top, #6366f1, #38bdf8)',
                                            animation: `waveform 1.2s ease-in-out ${i * 0.06}s infinite`,
                                            height: 4,
                                        }}
                                    />
                                ))}
                            </div>
                        )}
                    </div>

                    {/* Conversation Section */}
                    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>
                        <div style={{
                            padding: '12px 20px 8px',
                            fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
                            letterSpacing: 1.5, color: '#475569',
                        }}>Conversation</div>

                        <div style={{
                            flex: 1, overflowY: 'auto', padding: '4px 16px 16px',
                            display: 'flex', flexDirection: 'column', gap: 10,
                        }}>
                            {chatHistory.length === 0 ? (
                                <div style={{
                                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                                    height: '100%', color: '#1e293b', fontSize: 12, textAlign: 'center',
                                    padding: '0 20px', lineHeight: 1.6,
                                }}>
                                    <span style={{ color: '#334155' }}>
                                        💬 Conversation will appear here once you start speaking...
                                    </span>
                                </div>
                            ) : chatHistory.map((msg, i) => (
                                <div key={i} style={{
                                    display: 'flex',
                                    justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start',
                                }}>
                                    {msg.role === 'bot' && (
                                        <div style={{
                                            width: 24, height: 24, borderRadius: '50%',
                                            background: 'rgba(99, 102, 241, 0.15)',
                                            display: 'flex', alignItems: 'center', justifyContent: 'center',
                                            fontSize: 11, marginRight: 8, flexShrink: 0, marginTop: 2,
                                        }}>🤖</div>
                                    )}
                                    <div style={{
                                        maxWidth: '80%', padding: '8px 14px', borderRadius: 14, fontSize: 12.5, lineHeight: 1.55,
                                        background: msg.role === 'user'
                                            ? 'linear-gradient(135deg, #6366f1, #4f46e5)'
                                            : msg.kind === 'status'
                                                ? 'rgba(59, 130, 246, 0.08)'
                                                : 'rgba(255, 255, 255, 0.04)',
                                        color: msg.role === 'user' ? '#fff' : msg.kind === 'status' ? '#93c5fd' : '#94a3b8',
                                        border: msg.role === 'bot'
                                            ? msg.kind === 'status'
                                                ? '1px solid rgba(59, 130, 246, 0.18)'
                                                : '1px solid rgba(255, 255, 255, 0.06)'
                                            : 'none',
                                        borderBottomRightRadius: msg.role === 'user' ? 4 : 14,
                                        borderBottomLeftRadius: msg.role === 'bot' ? 4 : 14,
                                        fontStyle: msg.kind === 'status' ? 'italic' : 'normal',
                                    }}>
                                        {msg.text}
                                    </div>
                                    {msg.role === 'user' && (
                                        <div style={{
                                            width: 24, height: 24, borderRadius: '50%',
                                            background: 'rgba(99, 102, 241, 0.3)',
                                            display: 'flex', alignItems: 'center', justifyContent: 'center',
                                            fontSize: 11, marginLeft: 8, flexShrink: 0, marginTop: 2,
                                        }}>👤</div>
                                    )}
                                </div>
                            ))}
                            <div ref={chatEndRef} />
                        </div>
                    </div>

                    {/* Tip box (idle only) */}
                    {status === 'idle' && (
                        <div style={{
                            margin: '0 16px 16px',
                            background: 'rgba(99, 102, 241, 0.06)',
                            border: '1px solid rgba(99, 102, 241, 0.12)',
                            borderRadius: 12, padding: '14px 16px',
                        }}>
                            <div style={{ fontSize: 10, fontWeight: 600, color: '#818cf8', marginBottom: 6, textTransform: 'uppercase', letterSpacing: 1 }}>
                                Try saying
                            </div>
                            <p style={{
                                margin: 0, fontSize: 12, color: '#6366f1',
                                fontStyle: 'italic', lineHeight: 1.5,
                            }}>
                                "Find me a luxury boutique hotel in Paris with a balcony view of the Eiffel Tower for next weekend."
                            </p>
                        </div>
                    )}
                </div>

                {/* ── RIGHT PANEL (72%) — Browser View ── */}
                <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', background: '#060611' }}>

                    {/* Live Transcript Strip */}
                    <div style={{
                        display: 'none',
                        height: 40, minHeight: 40,
                        alignItems: 'center', gap: 16,
                        padding: '0 20px',
                        background: 'rgba(15, 15, 30, 0.95)',
                        borderBottom: '1px solid rgba(99, 102, 241, 0.08)',
                        overflow: 'hidden',
                    }}>
                        <div style={{
                            fontSize: 10, fontWeight: 600, textTransform: 'uppercase',
                            letterSpacing: 1.2, color: '#475569', flexShrink: 0,
                            display: 'flex', alignItems: 'center', gap: 6,
                        }}>
                            <span style={{ fontSize: 12 }}>💬</span>
                            Live Transcript
                        </div>
                        <div style={{
                            flex: 1, display: 'flex', alignItems: 'center', gap: 12,
                            overflow: 'hidden', fontSize: 12, minWidth: 0,
                        }}>
                            {lastUserMsg && (
                                <span style={{
                                    color: '#818cf8', whiteSpace: 'nowrap',
                                    minWidth: 0,
                                    overflow: 'hidden', textOverflow: 'ellipsis',
                                }}>
                                    "{lastUserMsg}"
                                </span>
                            )}
                            {lastBotMsg && (
                                <>
                                    <span style={{ color: '#1e293b' }}>›</span>
                                    <span style={{
                                        color: '#64748b', whiteSpace: 'nowrap',
                                        minWidth: 0,
                                        overflow: 'hidden', textOverflow: 'ellipsis',
                                    }}>
                                        "{lastBotMsg}"
                                    </span>
                                </>
                            )}
                            {!lastUserMsg && !lastBotMsg && (
                                <span style={{ color: '#1e293b', fontStyle: 'italic' }}>
                                    Waiting for voice input...
                                </span>
                            )}
                        </div>
                    </div>

                    {/* Browser viewport */}
                    <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>

                        {isBrowserActive ? (
                            <div style={{
                                flex: 1,
                                minWidth: 0,
                                display: 'flex',
                                alignItems: 'center',
                                justifyContent: 'center',
                                overflow: 'hidden',
                                background: '#0a0a1a',
                            }}>
                                <canvas
                                    ref={canvasRef}
                                    width={BROWSER_VIEWPORT_WIDTH}
                                    height={BROWSER_VIEWPORT_HEIGHT}
                                    tabIndex={0}
                                    onClick={handleCanvasClick}
                                    onKeyDown={handleCanvasKeyDown}
                                    style={{
                                        width: '100%',
                                        height: 'auto',
                                        maxHeight: '100%',
                                        aspectRatio: `${BROWSER_VIEWPORT_WIDTH} / ${BROWSER_VIEWPORT_HEIGHT}`,
                                        display: 'block',
                                        cursor: 'pointer',
                                        outline: 'none',
                                        background: '#0a0a1a',
                                    }}
                                />
                            </div>
                        ) : (
                            <div style={{
                                display: 'flex', flexDirection: 'column', alignItems: 'center',
                                justifyContent: 'center', height: '100%', gap: 20,
                            }}>
                                <div style={{
                                    fontSize: 64, opacity: 0.08,
                                    animation: 'float 3s ease-in-out infinite',
                                }}>🌐</div>
                                <div style={{ textAlign: 'center' }}>
                                    <p style={{
                                        margin: '0 0 6px', fontSize: 16, fontWeight: 600,
                                        color: '#334155',
                                    }}>
                                        {status === 'connected'
                                            ? 'Speak to start searching'
                                            : 'Connect to start'}
                                    </p>
                                    <p style={{ margin: 0, fontSize: 12, color: '#1e293b' }}>
                                        {status === 'connected'
                                            ? 'The browser will open once the AI starts searching'
                                            : 'Click the microphone button to initiate a voice command'}
                                    </p>
                                </div>
                            </div>
                        )}

                        {/* Loading overlay (browser active but no frame yet) */}
                        {isBrowserActive && !hasBrowserFrame && (
                            <div style={{
                                position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
                                alignItems: 'center', justifyContent: 'center', gap: 16,
                                background: 'rgba(6, 6, 17, 0.85)',
                                backdropFilter: 'blur(4px)',
                            }}>
                                {/* Spinning gear */}
                                <div style={{
                                    width: 56, height: 56, borderRadius: 14,
                                    background: 'rgba(99, 102, 241, 0.1)',
                                    border: '1px solid rgba(99, 102, 241, 0.2)',
                                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                                }}>
                                    <div style={{ fontSize: 28, animation: 'spin 1.5s linear infinite' }}>⚙️</div>
                                </div>
                                <div style={{ textAlign: 'center' }}>
                                    <p style={{ margin: '0 0 4px', fontSize: 15, fontWeight: 600, color: '#e2e8f0' }}>
                                        Connecting to browser...
                                    </p>
                                    <p style={{ margin: 0, fontSize: 12, color: '#64748b' }}>
                                        Navigating to booking.com
                                    </p>
                                </div>

                                {/* Protocol telemetry badges */}
                                <div style={{
                                    display: 'flex', gap: 12, marginTop: 8,
                                }}>
                                    {[
                                        { label: 'Protocol', value: 'CDP Screencast', color: '#6366f1' },
                                        { label: 'Transport', value: 'WebSocket', color: '#38bdf8' },
                                        { label: 'Status', value: 'Handshake...', color: '#f59e0b' },
                                    ].map(({ label, value, color }) => (
                                        <div key={label} style={{
                                            background: 'rgba(255, 255, 255, 0.03)',
                                            border: '1px solid rgba(255, 255, 255, 0.06)',
                                            borderRadius: 8, padding: '6px 12px',
                                            textAlign: 'center',
                                        }}>
                                            <div style={{ fontSize: 9, color: '#475569', textTransform: 'uppercase', letterSpacing: 1, marginBottom: 2 }}>
                                                {label}
                                            </div>
                                            <div style={{ fontSize: 11, color, fontWeight: 600 }}>
                                                {value}
                                            </div>
                                        </div>
                                    ))}
                                </div>

                                {/* Progress bar */}
                                <div style={{
                                    width: 200, height: 3, background: 'rgba(255, 255, 255, 0.05)',
                                    borderRadius: 3, overflow: 'hidden', marginTop: 4,
                                }}>
                                    <div style={{
                                        height: '100%', borderRadius: 3,
                                        background: 'linear-gradient(90deg, #6366f1, #38bdf8)',
                                        animation: 'loadBar 2s ease-in-out infinite',
                                    }} />
                                </div>
                            </div>
                        )}
                    </div>

                    {/* Bottom status bar */}
                    <div style={{
                        height: 28, minHeight: 28,
                        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                        padding: '0 20px',
                        background: 'rgba(10, 10, 22, 0.95)',
                        borderTop: '1px solid rgba(99, 102, 241, 0.06)',
                        fontSize: 10, color: '#334155',
                    }}>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
                            <span>Powered by <strong style={{ color: '#475569' }}>Amazon Nova Sonic</strong></span>
                            <span style={{ color: '#1e293b' }}>•</span>
                            <span>Vision-driven automation</span>
                        </div>
                        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                            {isBrowserActive && (
                                <span style={{ color: '#38bdf8' }}>● Streaming</span>
                            )}
                            <span>v1.0</span>
                        </div>
                    </div>
                </div>
            </div>

            <style>{`
                @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
                * { box-sizing: border-box; margin: 0; padding: 0; }
                body { margin: 0; overflow: hidden; }
                ::-webkit-scrollbar { width: 4px; }
                ::-webkit-scrollbar-thumb { background: rgba(99,102,241,0.25); border-radius: 4px; }
                ::-webkit-scrollbar-track { background: transparent; }
                @keyframes ping { 0%,100%{transform:scale(1);opacity:.4}50%{transform:scale(1.3);opacity:.08} }
                @keyframes pulse { 0%,100%{opacity:1}50%{opacity:.3} }
                @keyframes spin { from{transform:rotate(0deg)}to{transform:rotate(360deg)} }
                @keyframes float { 0%,100%{transform:translateY(0)}50%{transform:translateY(-8px)} }
                @keyframes waveform {
                    0%,100% { height: 4px; }
                    25% { height: ${Math.random() * 12 + 6}px; }
                    50% { height: ${Math.random() * 16 + 8}px; }
                    75% { height: ${Math.random() * 10 + 4}px; }
                }
                @keyframes loadBar {
                    0% { width: 0%; margin-left: 0; }
                    50% { width: 70%; margin-left: 15%; }
                    100% { width: 0%; margin-left: 100%; }
                }
            `}</style>
        </div>
    );
}
