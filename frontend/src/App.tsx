import { useEffect, useMemo, useState } from "react";
import { AudioConference, LiveKitRoom } from "@livekit/components-react";
import type { LiveKitRoomProps } from "@livekit/components-react";

interface LiveKitSession {
  identity: string;
  room: string;
  url: string;
  token: string;
  agent_identity?: string;
}

type FetchState = "loading" | "ready" | "error" | "missing";

const statusMessages: Record<FetchState, string> = {
  loading: "Connecting to the voice assistant…",
  missing: "Waiting for the voice assistant to start a LiveKit session.",
  error: "Unable to load the LiveKit session. Check the server logs for details.",
  ready: "",
};

async function loadSession(): Promise<LiveKitSession | null> {
  const response = await fetch("/api/livekit/session");
  if (response.status === 404) {
    return null;
  }
  if (!response.ok) {
    throw new Error(`Request failed with status ${response.status}`);
  }
  return (await response.json()) as LiveKitSession;
}

export default function App() {
  const [session, setSession] = useState<LiveKitSession | null>(null);
  const [state, setState] = useState<FetchState>("loading");

  useEffect(() => {
    let mounted = true;

    async function fetchSession() {
      try {
        const data = await loadSession();
        if (!mounted) {
          return;
        }
        if (!data) {
          setState("missing");
          setSession(null);
          return;
        }
        setSession(data);
        setState("ready");
      } catch (error) {
        console.error("Failed to load LiveKit session", error);
        if (mounted) {
          setState("error");
        }
      }
    }

    fetchSession();

    const interval = window.setInterval(fetchSession, 5000);
    return () => {
      mounted = false;
      window.clearInterval(interval);
    };
  }, []);

  const roomProps: Partial<LiveKitRoomProps> = useMemo(() => {
    if (!session) {
      return {};
    }
    return {
      token: session.token,
      serverUrl: session.url,
      audio: true,
      video: false,
      connect: true,
    } satisfies Partial<LiveKitRoomProps>;
  }, [session]);

  if (!session || state !== "ready") {
    return (
      <main className="status">
        <div className="card">
          <h1>Codex Belya</h1>
          <p>{statusMessages[state]}</p>
        </div>
      </main>
    );
  }

  return (
    <main className="room">
      <header className="room__header">
        <div>
          <h1>Codex Belya Live Session</h1>
          <p>
            Listening to <strong>{session.agent_identity ?? "Codex Belya"}</strong> as <strong>{session.identity}</strong> in room <strong>{session.room}</strong>
          </p>
        </div>
      </header>
      <section className="room__content" data-lk-theme="default">
        <LiveKitRoom {...roomProps}>
          <AudioConference />
        </LiveKitRoom>
      </section>
    </main>
  );
}
