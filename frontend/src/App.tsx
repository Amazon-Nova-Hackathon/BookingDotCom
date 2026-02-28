import { useState, useRef, useCallback, useEffect } from 'react';

type ConnectionStatus = 'idle' | 'connecting' | 'connected';

interface BookingParams {
    destination?: string;
    checkin_date?: string;
    checkout_date?: string;
    adults?: number;
}

interface ChatMessage {
    role: 'user' | 'bot';
    text: string;
}

export default function App() {
    const [status, setStatus] = useState<ConnectionStatus>('idle');
    const [chatHistory, setChatHistory] = useState<ChatMessage[]>([]);
    const [bookingParams, setBookingParams] = useState<BookingParams>({});
    const [isBrowserActive, setIsBrowserActive] = useState(false);
    const [screenshotUrl, setScreenshotUrl] = useState<string | null>(null);
    const [browserStatus, setBrowserStatus] = useState('Chờ yêu cầu tìm kiếm...');
    const [isMuted, setIsMuted] = useState(false);

    const pcRef = useRef<RTCPeerConnection | null>(null);
    const pcIdRef = useRef<string | null>(null);
    const mediaStreamRef = useRef<MediaStream | null>(null);
    const remoteAudioRef = useRef<HTMLAudioElement | null>(null);
    const chatEndRef = useRef<HTMLDivElement>(null);
    const eventSourceRef = useRef<EventSource | null>(null);
    const pollIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
    const sseReconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
    const disconnectedRef = useRef(true); // start as "disconnected" so polling doesn't auto-activate browser
    const screenshotImgRef = useRef<HTMLImageElement>(null);
    const fetchingScreenshotRef = useRef(false); // guard against overlapping requests

    useEffect(() => {
        chatEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }, [chatHistory]);

    // ── Screenshot polling ─────────────────────────────────────────────────────
    const startScreenshotPolling = useCallback(() => {
        if (pollIntervalRef.current) return;
        console.log('[Browser] Starting screenshot polling');
        pollIntervalRef.current = setInterval(async () => {
            // Don't activate browser when intentionally disconnected
            if (disconnectedRef.current) return;
            // Skip if a previous fetch is still in-flight
            if (fetchingScreenshotRef.current) return;
            fetchingScreenshotRef.current = true;
            try {
                const res = await fetch('/api/screenshot', { cache: 'no-store' });
                if (res.status === 200 && res.headers.get('content-type')?.includes('image')) {
                    const blob = await res.blob();
                    if (blob.size > 100) {
                        const url = URL.createObjectURL(blob);
                        setScreenshotUrl(prev => {
                            if (prev) URL.revokeObjectURL(prev);
                            return url;
                        });
                        setIsBrowserActive(true);
                    }
                }
            } catch { /* browser service not running */ }
            finally { fetchingScreenshotRef.current = false; }
        }, 800);
    }, []);

    const stopScreenshotPolling = useCallback(() => {
        if (pollIntervalRef.current) {
            clearInterval(pollIntervalRef.current);
            pollIntervalRef.current = null;
        }
    }, []);

    // ── SSE ───────────────────────────────────────────────────────────────────
    const connectSSE = useCallback(() => {
        if (eventSourceRef.current) return;
        if (disconnectedRef.current) return;
        console.log('[SSE] Connecting to /api/events...');
        const es = new EventSource('/api/events');
        eventSourceRef.current = es;

        es.onopen = () => console.log('[SSE] Connected successfully');

        es.onmessage = (evt) => {
            try {
                const data = JSON.parse(evt.data);
                console.log('[SSE] Event received:', data.type, data);
                switch (data.type) {
                    case 'user_transcript':
                        setChatHistory(h => [...h, { role: 'user', text: data.text }]);
                        break;
                    case 'bot_response':
                        setChatHistory(h => {
                            const last = h[h.length - 1];
                            if (last?.role === 'bot') {
                                return [...h.slice(0, -1), { role: 'bot', text: last.text + data.text }];
                            }
                            return [...h, { role: 'bot', text: data.text }];
                        });
                        break;
                    case 'tool_called':
                        console.log('[SSE] Tool called! Starting browser view...');
                        setBookingParams(data.args ?? {});
                        setIsBrowserActive(true);
                        setBrowserStatus('🤖 Đang thao tác trên Booking.com...');
                        break;
                    case 'tool_result':
                        console.log('[SSE] Tool result received');
                        setBrowserStatus(data.error ? '❌ Tìm kiếm thất bại' : '✅ Tìm kiếm hoàn tất');
                        break;
                }
            } catch { }
        };

        es.onerror = (err) => {
            console.log('[SSE] Error, will reconnect in 2s', err);
            es.close();
            eventSourceRef.current = null;
            if (!disconnectedRef.current) {
                sseReconnectTimer.current = setTimeout(connectSSE, 2000);
            }
        };
    }, []);

    useEffect(() => {
        // Start polling on mount (but disconnectedRef=true prevents browser activation)
        startScreenshotPolling();
        return () => {
            eventSourceRef.current?.close();
            stopScreenshotPolling();
            if (sseReconnectTimer.current) clearTimeout(sseReconnectTimer.current);
        };
    }, [stopScreenshotPolling, startScreenshotPolling]);

    // ── WebRTC ────────────────────────────────────────────────────────────────
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

    // ── Disconnect ────────────────────────────────────────────────────────────
    const disconnect = useCallback(() => {
        console.log('[Disconnect] Stopping connection...');
        disconnectedRef.current = true;

        // Stop mic tracks
        mediaStreamRef.current?.getTracks().forEach(t => t.stop());
        mediaStreamRef.current = null;

        // Stop remote audio
        if (remoteAudioRef.current) {
            remoteAudioRef.current.pause();
            remoteAudioRef.current.srcObject = null;
            remoteAudioRef.current = null;
        }

        // Close peer connection
        const pc = pcRef.current;
        if (pc) {
            pc.ontrack = null;
            pc.onicecandidate = null;
            pc.onconnectionstatechange = null;
            pc.close();
        }
        pcRef.current = null;
        pcIdRef.current = null;

        // Close SSE and cancel reconnect timer
        if (eventSourceRef.current) {
            eventSourceRef.current.close();
            eventSourceRef.current = null;
        }
        if (sseReconnectTimer.current) {
            clearTimeout(sseReconnectTimer.current);
            sseReconnectTimer.current = null;
        }

        // Reset ALL UI state
        setStatus('idle');
        setIsMuted(false);
        setIsBrowserActive(false);
        setScreenshotUrl(prev => {
            if (prev) URL.revokeObjectURL(prev);
            return null;
        });
        setBrowserStatus('Chờ yêu cầu tìm kiếm...');
        console.log('[Disconnect] Done');
    }, []);

    // ── Start ─────────────────────────────────────────────────────────────────
    const startInteraction = async () => {
        try {
            disconnectedRef.current = false; // allow SSE & screenshot activation
            setStatus('connecting');
            setChatHistory([]);
            setBookingParams({});
            setIsBrowserActive(false);
            setScreenshotUrl(null);
            setBrowserStatus('Chờ yêu cầu tìm kiếm...');
            setIsMuted(false);

            // Reconnect SSE
            connectSSE();
            startScreenshotPolling();

            const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
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
                console.log('[WebRTC] Connection state:', state);
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

    // ── Mute toggle ───────────────────────────────────────────────────────────
    const toggleMute = () => {
        const stream = mediaStreamRef.current;
        if (!stream) return;
        const audioTrack = stream.getAudioTracks()[0];
        if (!audioTrack) return;
        audioTrack.enabled = !audioTrack.enabled;
        setIsMuted(!audioTrack.enabled);
        console.log('[Mute]', audioTrack.enabled ? 'Unmuted' : 'Muted');
    };

    // ── Browser interaction helpers ───────────────────────────────────────────
    const sendBrowserAction = async (action: string, payload: Record<string, unknown>) => {
        try {
            await fetch('/api/browser-interact', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action, ...payload }),
            });
        } catch (e) {
            console.warn('[Browser Interact]', e);
        }
    };

    const handleBrowserClick = (e: React.MouseEvent<HTMLImageElement>) => {
        const img = screenshotImgRef.current;
        if (!img) return;
        const rect = img.getBoundingClientRect();
        // Map click position to the actual browser viewport (1280x800)
        const scaleX = 1280 / rect.width;
        const scaleY = 800 / rect.height;
        const x = Math.round((e.clientX - rect.left) * scaleX);
        const y = Math.round((e.clientY - rect.top) * scaleY);
        console.log(`[Browser Click] (${x}, ${y})`);
        sendBrowserAction('click', { x, y });
    };

    const handleBrowserScroll = (e: React.WheelEvent<HTMLImageElement>) => {
        e.preventDefault();
        const deltaY = Math.round(e.deltaY);
        console.log(`[Browser Scroll] deltaY=${deltaY}`);
        sendBrowserAction('scroll', { x: 640, y: 400, deltaX: 0, deltaY });
    };

    const handleBrowserKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
        const specialKeys = ['Enter', 'Tab', 'Escape', 'Backspace', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Delete', 'Home', 'End', 'PageUp', 'PageDown'];
        if (specialKeys.includes(e.key)) {
            e.preventDefault();
            sendBrowserAction('keypress', { key: e.key });
        } else if (e.key.length === 1 && !e.ctrlKey && !e.altKey && !e.metaKey) {
            e.preventDefault();
            sendBrowserAction('type', { text: e.key });
        }
    };

    const formatDate = (d?: string) => {
        if (!d) return null;
        const [y, m, day] = d.split('-');
        return `${day}/${m}/${y}`;
    };

    const filledCount = [bookingParams.destination, bookingParams.checkin_date, bookingParams.checkout_date, bookingParams.adults].filter(Boolean).length;

    return (
        <div style={{
            display: 'flex', height: '100vh',
            background: '#0d0d1f', color: '#e2e8f0',
            fontFamily: "'Inter', sans-serif", overflow: 'hidden',
        }}>

            {/* ── LEFT (30%) ── */}
            <div style={{
                width: '30%', minWidth: 280,
                display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
                gap: 28, padding: '28px 20px',
                background: 'linear-gradient(160deg, #0f0f1a 0%, #1a1a2e 100%)',
                borderRight: '1px solid rgba(99,102,241,0.18)',
            }}>
                {/* Branding */}
                <div style={{ textAlign: 'center' }}>
                    <div style={{ fontSize: 38, marginBottom: 6 }}>🏨</div>
                    <h1 style={{
                        fontSize: 20, fontWeight: 800, margin: 0,
                        background: 'linear-gradient(135deg, #818cf8, #38bdf8)',
                        WebkitBackgroundClip: 'text', WebkitTextFillColor: 'transparent',
                    }}>Booking Voice AI</h1>
                    <p style={{ fontSize: 11, color: '#475569', margin: '4px 0 0' }}>Powered by Amazon Nova Sonic</p>
                </div>

                {/* Mic + Mute buttons row */}
                <div style={{ display: 'flex', alignItems: 'center', gap: 16 }}>
                    {/* Main start/stop button */}
                    <div style={{ position: 'relative', display: 'flex', justifyContent: 'center', alignItems: 'center' }}>
                        {status === 'connected' && (
                            <div style={{
                                position: 'absolute', width: 136, height: 136, borderRadius: '50%',
                                background: 'rgba(16,185,129,0.12)', animation: 'ping 2s ease-in-out infinite',
                                pointerEvents: 'none',
                            }} />
                        )}
                        <button
                            id={status === 'idle' ? 'btn-start' : 'btn-stop'}
                            onClick={status === 'idle' ? startInteraction : disconnect}
                            style={{
                                position: 'relative', zIndex: 1,
                                width: 100, height: 100, borderRadius: '50%', border: 'none',
                                cursor: status === 'connecting' ? 'wait' : 'pointer',
                                display: 'flex', flexDirection: 'column', alignItems: 'center',
                                justifyContent: 'center', gap: 4, fontSize: 11, fontWeight: 700,
                                color: '#fff', transition: 'all 0.3s',
                                background: status === 'connected'
                                    ? 'linear-gradient(135deg,#ef4444,#dc2626)'
                                    : status === 'connecting'
                                        ? 'linear-gradient(135deg,#f59e0b,#d97706)'
                                        : 'linear-gradient(135deg,#6366f1,#4f46e5)',
                                boxShadow: status === 'connected'
                                    ? '0 0 40px rgba(239,68,68,0.5)'
                                    : '0 0 40px rgba(99,102,241,0.5)',
                            }}
                        >
                            <span style={{ fontSize: 24 }}>{status === 'connected' ? '🔴' : status === 'connecting' ? '⏳' : '🎙️'}</span>
                            <span>{status === 'connected' ? 'Ngắt' : status === 'connecting' ? 'Đang kết...' : 'Bắt Đầu'}</span>
                        </button>
                    </div>

                    {/* Mute button (only visible when connected) */}
                    {status === 'connected' && (
                        <button
                            onClick={toggleMute}
                            title={isMuted ? 'Bật mic' : 'Tắt mic'}
                            style={{
                                width: 48, height: 48, borderRadius: '50%', border: 'none',
                                cursor: 'pointer',
                                background: isMuted
                                    ? 'linear-gradient(135deg,#ef4444,#dc2626)'
                                    : 'linear-gradient(135deg,#10b981,#059669)',
                                color: '#fff', fontSize: 20,
                                display: 'flex', alignItems: 'center', justifyContent: 'center',
                                boxShadow: isMuted
                                    ? '0 0 16px rgba(239,68,68,0.4)'
                                    : '0 0 16px rgba(16,185,129,0.4)',
                                transition: 'all 0.3s',
                            }}
                        >
                            {isMuted ? '🔇' : '🔊'}
                        </button>
                    )}
                </div>

                {/* Connection state */}
                <div style={{
                    display: 'flex', alignItems: 'center', gap: 8, fontSize: 12,
                    color: status === 'connected' ? (isMuted ? '#f59e0b' : '#10b981') : status === 'connecting' ? '#f59e0b' : '#475569',
                }}>
                    <div style={{
                        width: 8, height: 8, borderRadius: '50%',
                        background: status === 'connected' ? (isMuted ? '#f59e0b' : '#10b981') : status === 'connecting' ? '#f59e0b' : '#1e293b',
                        boxShadow: status === 'connected' ? `0 0 8px ${isMuted ? '#f59e0b' : '#10b981'}` : undefined,
                    }} />
                    {status === 'connected'
                        ? (isMuted ? 'Mic tắt — Nhấn 🔊 để bật' : 'Đang kết nối — Hãy nói chuyện')
                        : status === 'connecting' ? 'Đang kết nối...' : 'Sẵn sàng'}
                </div>

                {/* Extracted params summary */}
                <div style={{ width: '100%', display: 'flex', flexDirection: 'column', gap: 8 }}>
                    {[
                        { label: '📍 Điểm đến', val: bookingParams.destination },
                        { label: '📅 Nhận phòng', val: formatDate(bookingParams.checkin_date) },
                        { label: '📅 Trả phòng', val: formatDate(bookingParams.checkout_date) },
                        { label: '👥 Người lớn', val: bookingParams.adults != null ? `${bookingParams.adults} người` : undefined },
                    ].map(({ label, val }) => (
                        <div key={label} style={{
                            display: 'flex', justifyContent: 'space-between', alignItems: 'center',
                            background: val ? 'rgba(99,102,241,0.1)' : 'rgba(255,255,255,0.03)',
                            border: `1px solid ${val ? 'rgba(99,102,241,0.3)' : 'rgba(255,255,255,0.06)'}`,
                            borderRadius: 8, padding: '8px 12px', fontSize: 12, transition: 'all 0.3s',
                        }}>
                            <span style={{ color: '#64748b' }}>{label}</span>
                            <span style={{ color: val ? '#a5b4fc' : '#1e293b', fontWeight: val ? 600 : 400 }}>
                                {val || '—'}
                            </span>
                        </div>
                    ))}

                    {filledCount > 0 && (
                        <div style={{ marginTop: 4 }}>
                            <div style={{ height: 3, background: 'rgba(255,255,255,0.05)', borderRadius: 3 }}>
                                <div style={{
                                    height: '100%', borderRadius: 3,
                                    background: 'linear-gradient(90deg,#6366f1,#38bdf8)',
                                    width: `${filledCount * 25}%`, transition: 'width 0.5s ease',
                                }} />
                            </div>
                            <div style={{ fontSize: 10, color: '#475569', marginTop: 4, textAlign: 'right' }}>
                                {filledCount}/4 thông tin
                            </div>
                        </div>
                    )}
                </div>

                {/* Instruction (idle only) */}
                {status === 'idle' && (
                    <div style={{
                        background: 'rgba(99,102,241,0.07)', border: '1px solid rgba(99,102,241,0.18)',
                        borderRadius: 10, padding: '12px 14px', fontSize: 11, color: '#64748b', lineHeight: 1.6,
                    }}>
                        <p style={{ margin: '0 0 6px', fontWeight: 600, color: '#818cf8' }}>💡 Ví dụ</p>
                        <p style={{ margin: 0, fontStyle: 'italic', color: '#6366f1' }}>
                            "Find a hotel in Paris, check in March 1st, check out March 3rd, 2 adults"
                        </p>
                    </div>
                )}
            </div>

            {/* ── RIGHT (70%) — Browser View ── */}
            <div style={{ width: '70%', display: 'flex', flexDirection: 'column', background: '#090915' }}>

                {/* Chat strip on top */}
                <div style={{
                    height: 180, borderBottom: '1px solid rgba(99,102,241,0.12)',
                    overflowY: 'auto', padding: '12px 20px', display: 'flex', flexDirection: 'column', gap: 8,
                    background: 'rgba(15,15,30,0.9)',
                }}>
                    {chatHistory.length === 0 ? (
                        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: '#1e293b', fontSize: 13 }}>
                            💬 Lịch sử hội thoại sẽ hiện ở đây
                        </div>
                    ) : chatHistory.map((msg, i) => (
                        <div key={i} style={{ display: 'flex', justifyContent: msg.role === 'user' ? 'flex-end' : 'flex-start' }}>
                            <div style={{
                                maxWidth: '75%', padding: '7px 12px', borderRadius: 12, fontSize: 12, lineHeight: 1.5,
                                background: msg.role === 'user' ? 'linear-gradient(135deg,#6366f1,#4f46e5)' : 'rgba(255,255,255,0.06)',
                                color: msg.role === 'user' ? '#fff' : '#94a3b8',
                                border: msg.role === 'bot' ? '1px solid rgba(255,255,255,0.07)' : 'none',
                                borderBottomRightRadius: msg.role === 'user' ? 3 : 12,
                                borderBottomLeftRadius: msg.role === 'bot' ? 3 : 12,
                            }}>
                                <span style={{ fontSize: 10, opacity: 0.5, marginRight: 6 }}>
                                    {msg.role === 'user' ? '🎙️' : '🤖'}
                                </span>
                                {msg.text}
                            </div>
                        </div>
                    ))}
                    <div ref={chatEndRef} />
                </div>

                {/* Browser viewport */}
                <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden' }}>

                    {/* Status bar — only when browser is active */}
                    {isBrowserActive && (
                        <div style={{
                            padding: '10px 20px', display: 'flex', alignItems: 'center', gap: 10,
                            background: 'rgba(0,0,0,0.3)', borderBottom: '1px solid rgba(255,255,255,0.05)',
                            fontSize: 12, color: '#f59e0b',
                        }}>
                            <div style={{
                                width: 8, height: 8, borderRadius: '50%',
                                background: '#f59e0b',
                                boxShadow: '0 0 8px #f59e0b',
                                animation: 'pulse 1.2s ease-in-out infinite',
                            }} />
                            <span style={{ fontFamily: 'monospace', fontSize: 11, color: '#475569' }}>
                                🌐 https://www.booking.com
                            </span>
                            <span style={{ marginLeft: 'auto' }}>{browserStatus}</span>
                            <span style={{ fontSize: 10, color: '#475569', marginLeft: 8 }}>
                                💡 Click / cuộn / gõ phím để tương tác
                            </span>
                        </div>
                    )}

                    {/* Screenshot display / Interactive browser */}
                    <div
                        style={{ flex: 1, overflow: 'hidden', position: 'relative', background: '#060611', outline: 'none' }}
                        tabIndex={0}
                        onKeyDown={isBrowserActive && screenshotUrl ? handleBrowserKeyDown : undefined}
                    >
                        {isBrowserActive && screenshotUrl ? (
                            <img
                                ref={screenshotImgRef}
                                src={screenshotUrl}
                                alt="Live browser view — click to interact"
                                onClick={handleBrowserClick}
                                onWheel={handleBrowserScroll}
                                style={{
                                    width: '100%', height: '100%', objectFit: 'contain',
                                    display: 'block', cursor: 'pointer',
                                }}
                            />
                        ) : (
                            <div style={{
                                display: 'flex', flexDirection: 'column', alignItems: 'center',
                                justifyContent: 'center', height: '100%', gap: 16, color: '#1e293b',
                            }}>
                                <div style={{ fontSize: 56, opacity: 0.15 }}>🌐</div>
                                <p style={{ margin: 0, fontSize: 14, color: '#1e293b' }}>
                                    {status === 'connected'
                                        ? 'Hãy nói để bắt đầu tìm kiếm khách sạn'
                                        : 'Kết nối để bắt đầu'}
                                </p>
                                {status !== 'connected' && (
                                    <p style={{ margin: 0, fontSize: 11, color: '#0f172a' }}>
                                        Trình duyệt sẽ tự động mở khi bot tìm kiếm
                                    </p>
                                )}
                            </div>
                        )}

                        {/* Searching overlay */}
                        {isBrowserActive && !screenshotUrl && (
                            <div style={{
                                position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
                                alignItems: 'center', justifyContent: 'center', gap: 12,
                                background: 'rgba(0,0,0,0.4)',
                            }}>
                                <div style={{ fontSize: 32, animation: 'spin 1s linear infinite' }}>⚙️</div>
                                <p style={{ margin: 0, fontSize: 14, color: '#f59e0b' }}>Đang tải Booking.com...</p>
                            </div>
                        )}
                    </div>
                </div>
            </div>

            <style>{`
        @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { margin: 0; overflow: hidden; }
        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-thumb { background: rgba(99,102,241,0.3); border-radius: 4px; }
        @keyframes ping { 0%,100%{transform:scale(1);opacity:.5}50%{transform:scale(1.15);opacity:.15} }
        @keyframes pulse { 0%,100%{opacity:1}50%{opacity:.3} }
        @keyframes spin { from{transform:rotate(0deg)}to{transform:rotate(360deg)} }
      `}</style>
        </div>
    );
}
