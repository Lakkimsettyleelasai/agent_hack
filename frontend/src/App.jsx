import React, { useState, useRef, useEffect } from 'react';
import ReactMarkdown from 'react-markdown';
import './index.css';

function App() {
  const [messages, setMessages] = useState([]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [chatMode, setChatMode] = useState('agent');
  const [autoApprove, setAutoApprove] = useState(false);
  const [pendingApproval, setPendingApproval] = useState(null);
  const chatEndRef = useRef(null);

  const handleApproval = async (approved) => {
    if (!pendingApproval) return;
    try {
      await fetch('http://localhost:8000/chat/approve', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_id: pendingApproval.task_id, approved })
      });
      setPendingApproval(null);
    } catch (err) {
      console.error("Error sending approval:", err);
    }
  };

  const scrollToBottom = () => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  const handleSubmit = async (e) => {
    e.preventDefault();
    if (!input.trim() || isLoading) return;

    const userMsg = { role: 'user', content: input };
    setMessages(prev => [...prev, userMsg]);
    setInput('');
    setIsLoading(true);

    const startTime = Date.now();
    setMessages(prev => [
      ...prev,
      {
        role: 'agent',
        content: '',
        model: 'Unknown',
        reasoning_steps: [],
        startTime: startTime,
        responseTime: null
      }
    ]);

    const validHistory = messages
      .filter(msg => typeof msg.content === 'string' && msg.content.trim() !== '' && msg.role !== 'sys')
      .map(msg => ({ role: msg.role, content: msg.content }));

    try {
      const response = await fetch('http://localhost:8000/chat/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: userMsg.content, history: validHistory, mode: chatMode, auto_approve: autoApprove })
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder('utf-8');
      let done = false;
      let buffer = '';

      while (!done) {
        const { value, done: readerDone } = await reader.read();
        done = readerDone;

        if (value) {
          buffer += decoder.decode(value, { stream: true });
          const events = buffer.split('\n\n');
          buffer = events.pop(); // Keep the last incomplete chunk in the buffer

          for (const event of events) {
            const lines = event.split('\n');
            for (const line of lines) {
              if (line.startsWith('data: ')) {
                const dataStr = line.slice(6);
                try {
                  const data = JSON.parse(dataStr);
                  
                  if (data.type === 'meta') {
                    setMessages(prev => {
                      const newMsgs = [...prev];
                      if (newMsgs.length > 0) {
                        newMsgs[newMsgs.length - 1].model = data.model;
                      }
                      return newMsgs;
                    });
                  } else if (data.type === 'approval_request') {
                    setPendingApproval({ command: data.command, task_id: data.task_id });
                  } else if (data.type === 'step') {
                    setMessages(prev => {
                      return prev.map((msg, idx) => {
                        if (idx !== prev.length - 1) return msg;
                        const steps = [...(msg.reasoning_steps || [])];
                        const obsText = data.content.observation || data.content.observations;
                        if (obsText && steps.length > 0) {
                          steps[steps.length - 1] = { ...steps[steps.length - 1], observations: obsText };
                        } else {
                          steps.push(data.content);
                        }
                        return { ...msg, reasoning_steps: steps };
                      });
                    });
                  } else if (data.type === 'content') {
                    setMessages(prev => {
                      const newMsgs = [...prev];
                      if (newMsgs.length > 0) {
                        newMsgs[newMsgs.length - 1].content = (newMsgs[newMsgs.length - 1].content || '') + data.content;
                      }
                      return newMsgs;
                    });
                  } else if (data.type === 'final_answer') {
                    setMessages(prev => {
                      const newMsgs = [...prev];
                      if (newMsgs.length > 0) {
                        const strContent = typeof data.content === 'object' ? JSON.stringify(data.content, null, 2) : String(data.content);
                        newMsgs[newMsgs.length - 1].content = strContent;
                      }
                      return newMsgs;
                    });
                  } else if (data.type === 'error') {
                    setMessages(prev => {
                      const newMsgs = [...prev];
                      if (newMsgs.length > 0) {
                        newMsgs[newMsgs.length - 1].content = `**Error from Agent**: ${data.content}`;
                      }
                      return newMsgs;
                    });
                  }
                } catch (e) {
                  console.error("Error parsing JSON chunk:", e, dataStr);
                }
              }
            }
          }
        }
      }
      setMessages(prev => {
        const newMsgs = [...prev];
        if (newMsgs.length > 0) {
          const lastMsg = newMsgs[newMsgs.length - 1];
          if (lastMsg.role === 'agent' && lastMsg.startTime) {
            lastMsg.responseTime = ((Date.now() - lastMsg.startTime) / 1000).toFixed(1);
          }
        }
        return newMsgs;
      });
      setIsLoading(false);
    } catch (error) {
      console.error('Error fetching chat response:', error);
      setMessages(prev => {
        const newMsgs = [...prev];
        if (newMsgs.length > 0) {
           newMsgs[newMsgs.length - 1].content = '**Error**: Could not connect to the Agentic framework or the connection timed out. (' + error.message + ')';
        }
        return newMsgs;
      });
      setIsLoading(false);
    }
  };

  const handleNewChat = () => {
    setMessages([]);
    setInput('');
  };

  return (
    <div className="app-container">
      {/* Sidebar */}
      <div className="sidebar">
        <div className="sidebar-header">
          <div className="logo"></div>
          <div>
            <h2>Terminal</h2>
            <div className="subtitle">Agentic Mode</div>
          </div>
        </div>
        
        <button onClick={handleNewChat} className="new-chat-btn">
          + New Session
        </button>

        <div className="mode-toggle-container">
          <label className="toggle-switch">
            <input 
              type="checkbox" 
              checked={chatMode === 'direct'}
              onChange={(e) => setChatMode(e.target.checked ? 'direct' : 'agent')}
            />
            <span className="slider"></span>
          </label>
          <span className="mode-label">{chatMode === 'agent' ? 'Agent Mode (Qwen)' : 'Direct Chat (Dolphin)'}</span>
        </div>

        <div className="mode-toggle-container auto-approve-container">
          <label className="toggle-switch">
            <input 
              type="checkbox" 
              checked={autoApprove}
              onChange={(e) => setAutoApprove(e.target.checked)}
            />
            <span className="slider auto-approve-slider"></span>
          </label>
          <span className="mode-label" style={{color: autoApprove ? '#2ea043' : ''}}>
            {autoApprove ? 'Auto-Approve: ON' : 'Auto-Approve: OFF'}
          </span>
        </div>
      </div>

      {/* Main Chat Interface */}
      <div className="main-content">
        <div className="chat-window">
          {messages.length === 0 ? (
            <div className="message-wrapper agent">
              <div className="message">
                <div className="avatar">SYS</div>
                <div className="content">
                  <p>SYSTEM INITIALIZED. Agent is ready.</p>
                </div>
              </div>
            </div>
          ) : (
            messages.map((msg, idx) => (
              <div key={idx} className={`message-wrapper ${msg.role}`}>
                <div className="message">
                  <div className="avatar">
                    {msg.role === 'user' ? 'U' : 'SYS'}
                  </div>
                  <div className="content">
                    {msg.role === 'agent' && msg.reasoning_steps && msg.reasoning_steps.length > 0 && (
                      <details className="thoughts-details">
                        <summary className="thoughts-summary">
                          <span>Agent Thinking Process</span>
                          <svg className="chevron-icon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                            <polyline points="6 9 12 15 18 9"></polyline>
                          </svg>
                        </summary>
                        <div className="thoughts-container">
                          {msg.reasoning_steps.map((step, sIdx) => (
                            <div key={sIdx} className="thought-step">
                              <div className="thought-header">
                                <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                                  <circle cx="12" cy="12" r="10"></circle>
                                  <polyline points="12 6 12 12 16 14"></polyline>
                                </svg>
                                Trace {sIdx + 1}
                              </div>
                              
                              {step.thought && (
                                <div className="thought-content">
                                  {step.thought}
                                </div>
                              )}

                              {step.tool_calls && step.tool_calls.map((tc, tcIdx) => (
                                <div key={tcIdx} className="tool-call-block">
                                  <div>[ EXECUTE ]: {tc.name}</div>
                                  <pre>{JSON.stringify(tc.arguments, null, 2)}</pre>
                                </div>
                              ))}

                              {(step.observation || step.observations) && (
                                <div className="observation-block">
                                  {step.observation || step.observations}
                                </div>
                              )}
                            </div>
                          ))}
                        </div>
                      </details>
                    )}
                    
                    {msg.role === 'agent' && msg.content && (
                      <div className="final-answer-container">
                        {msg.reasoning_steps && msg.reasoning_steps.length > 0 && (
                          <div className="final-answer-header">
                            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                              <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path>
                              <polyline points="22 4 12 14.01 9 11.01"></polyline>
                            </svg>
                            OUTPUT GENERATED
                          </div>
                        )}
                        <ReactMarkdown>{typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content)}</ReactMarkdown>
                      </div>
                    )}
                    
                    {msg.role === 'agent' && !msg.content && isLoading && idx === messages.length - 1 && (
                      <div className="loading">
                        <div className="dot"></div>
                        <div className="dot"></div>
                        <div className="dot"></div>
                        <span className="thinking-text">Processing stream...</span>
                      </div>
                    )}

                    {msg.role === 'user' && (
                      <ReactMarkdown>{typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content)}</ReactMarkdown>
                    )}

                    {msg.role === 'agent' && msg.model && (
                      <div className="meta-footer">
                        <span className="model-badge">Model: {msg.model}</span>
                        {msg.responseTime && (
                          <span className="response-time-badge">Time: {msg.responseTime}s</span>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </div>
            ))
          )}
          <div ref={chatEndRef} />
        </div>

        {/* Approval Banner */}
        {pendingApproval && (
          <div className="approval-banner">
            <div className="approval-content">
              <strong>⚠️ Agent requests permission to run:</strong>
              <div className="approval-command"><code>{pendingApproval.command}</code></div>
            </div>
            <div className="approval-actions">
              <button onClick={() => handleApproval(true)} className="approve-btn">Approve</button>
              <button onClick={() => handleApproval(false)} className="deny-btn">Deny</button>
            </div>
          </div>
        )}

        {/* Input Form */}
        <div className="input-container">
          <form className="input-form" onSubmit={handleSubmit}>
            <input
              type="text"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="Enter command..."
              disabled={isLoading || pendingApproval !== null}
            />
            <button type="submit" className="send-btn" disabled={isLoading || pendingApproval !== null || !input.trim()}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <line x1="22" y1="2" x2="11" y2="13"></line>
                <polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>
              </svg>
            </button>
          </form>
        </div>
      </div>
    </div>
  );
}

export default App;
