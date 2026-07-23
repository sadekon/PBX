import"./modulepreload-polyfill-Dezn_h7o.js";window.__DEV__=[`localhost`,`127.0.0.1`].includes(location.hostname)||![``,`80`,`443`].includes(location.port),window.debugLog=window.__DEV__?console.log.bind(console):function(){},window.debugWarn=window.__DEV__?console.warn.bind(console):function(){},window.addEventListener(`error`,function(e){console.error(`JavaScript Error:`,e.error||e.message,`
File:`,e.filename,`
Line:`,e.lineno)}),window.addEventListener(`unhandledrejection`,function(e){console.error(`Unhandled Promise Rejection:`,e.reason)}),debugLog(`Admin panel loading...`,new Date().toISOString()),(function(){var e=[`https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js`,`https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.0/chart.umd.min.js`,`https://unpkg.com/chart.js@4.4.0/dist/chart.umd.min.js`],t=0;function n(){if(t>=e.length){console.error(`All Chart.js CDN sources failed`),window.chartJsLoadFailed=!0;return}var r=document.createElement(`script`);r.src=e[t],r.onerror=function(){debugWarn(`Chart.js CDN `+(t+1)+` failed, trying next...`),t++,n()},r.onload=function(){debugLog(`Chart.js loaded successfully from CDN `+(t+1)),window.chartJsLoadFailed=!1},document.head.appendChild(r)}n()})(),window.API_BASE=(function(){var e=window.location.port;return e===`9000`||e===``||e===`80`||e===`443`?window.location.origin:window.location.protocol+`//`+(window.location.hostname||`localhost`)+`:9000`})(),window.pbxAuthHeaders=function(){var e={"Content-Type":`application/json`},t=localStorage.getItem(`pbx_token`);return t&&(e.Authorization=`Bearer `+t),e};function e(...e){window.WEBRTC_VERBOSE_LOGGING&&debugLog(`[VERBOSE]`,...e)}var t=class{constructor(t,n){this.apiUrl=t,this.extension=n,this.sessionId=null,this.callId=null,this.peerConnection=null,this.localStream=null,this.isCallActive=!1,this.remoteAudio=null,this._audioCtx=null,this._ringbackNodes=null,this._ringbackTimer=null,this._ringbackSafetyTimer=null,this._callStatusPollTimer=null,this._currentCallState=null,e(`WebRTC Phone constructor called:`,{apiUrl:this.apiUrl,extension:this.extension}),this.initializeUI()}_ensureAudioCtx(){return this._audioCtx||=new(window.AudioContext||window.webkitAudioContext),this._audioCtx.state===`suspended`&&this._audioCtx.resume(),this._audioCtx}startRingbackTone(){if(!this._ringbackNodes){this._ringbackSafetyTimer&&clearTimeout(this._ringbackSafetyTimer),this._ringbackSafetyTimer=setTimeout(()=>{debugLog(`[WebRTC Phone] Ringback safety timeout reached (60s), stopping`),this.stopRingbackTone()},6e4);try{let t=this._ensureAudioCtx(),n=t.createGain();n.gain.value=0,n.connect(t.destination);let r=t.createOscillator();r.frequency.value=440,r.connect(n),r.start();let i=t.createOscillator();i.frequency.value=480,i.connect(n),i.start(),this._ringbackNodes={osc1:r,osc2:i,gain:n};let a=2e3,o=4e3,s=!0;n.gain.value=.15,this._ringbackTimer=setInterval(()=>{s=!s,n.gain.value=s?.15:0},s?a:o),clearInterval(this._ringbackTimer);let c=()=>{this._ringbackNodes&&(this._ringbackNodes.gain.gain.value=.15,this._ringbackTimer=setTimeout(()=>{this._ringbackNodes&&(this._ringbackNodes.gain.gain.value=0,this._ringbackTimer=setTimeout(c,o))},a))};c(),e(`Ringback tone started`)}catch(e){console.error(`[WebRTC Phone] Error starting ringback tone:`,e)}}}stopRingbackTone(){if(this._ringbackSafetyTimer&&=(clearTimeout(this._ringbackSafetyTimer),null),this._ringbackTimer&&=(clearTimeout(this._ringbackTimer),null),this._ringbackNodes){try{this._ringbackNodes.osc1.stop(),this._ringbackNodes.osc2.stop(),this._ringbackNodes.gain.disconnect()}catch{}this._ringbackNodes=null,e(`Ringback tone stopped`)}}playDTMFTone(t){let n={1:[697,1209],2:[697,1336],3:[697,1477],4:[770,1209],5:[770,1336],6:[770,1477],7:[852,1209],8:[852,1336],9:[852,1477],"*":[941,1209],0:[941,1336],"#":[941,1477]}[t];if(n)try{let r=this._ensureAudioCtx(),i=r.createGain();i.gain.value=.15,i.connect(r.destination);let a=r.createOscillator();a.frequency.value=n[0],a.connect(i);let o=r.createOscillator();o.frequency.value=n[1],o.connect(i);let s=r.currentTime;a.start(s),o.start(s),a.stop(s+.15),o.stop(s+.15),i.gain.setValueAtTime(.15,s+.12),i.gain.linearRampToValueAtTime(0,s+.15),e(`DTMF tone played for digit:`,t)}catch(e){console.error(`[WebRTC Phone] Error playing DTMF tone:`,e)}}_startCallStatusPolling(){if(this._callStatusPollTimer)return;this._currentCallState=`calling`;let t=async()=>{if(!this.callId){this._stopCallStatusPolling();return}try{let t=await fetch(`${this.apiUrl}/api/webrtc/call-status?call_id=${encodeURIComponent(this.callId)}`,{headers:this.getAuthHeaders()});if(!t.ok)return;let n=await t.json();if(n.error)return;let r=n.status;if(r===this._currentCallState)return;e(`Call state changed:`,this._currentCallState,`->`,r);let i=this._currentCallState;this._currentCallState=r,r===`ringing`&&i===`calling`?(this.startRingbackTone(),this.updateStatus(`Ringing...`,`info`)):r===`connected`?(this.stopRingbackTone(),this.updateStatus(`Call connected`,`success`),this.updateUIState(`connected`)):r===`ended`&&(this.stopRingbackTone(),this._stopCallStatusPolling(),this.updateStatus(`Call ended by remote party`,`info`),this.hangup())}catch(t){e(`Call status poll error:`,t)}};this._callStatusPollTimer=setInterval(t,500),t()}_stopCallStatusPolling(){this._callStatusPollTimer&&=(clearInterval(this._callStatusPollTimer),null)}getAuthHeaders(){let e={"Content-Type":`application/json`},t=localStorage.getItem(`pbx_token`);return t&&(e.Authorization=`Bearer ${t}`),e}initializeUI(){this.callButton=document.getElementById(`webrtc-call-btn`),this.hangupButton=document.getElementById(`webrtc-hangup-btn`),this.muteButton=document.getElementById(`webrtc-mute-btn`),this.volumeSlider=document.getElementById(`webrtc-volume`),this.statusDiv=document.getElementById(`webrtc-status`),this.targetExtension=document.getElementById(`webrtc-target-ext`),this.keypadSection=document.getElementById(`webrtc-keypad-section`),this.remoteAudio=document.getElementById(`webrtc-remote-audio`),this.remoteAudio||(this.remoteAudio=document.createElement(`audio`),this.remoteAudio.id=`webrtc-remote-audio`,this.remoteAudio.autoplay=!0,this.remoteAudio.playsInline=!0,document.body.appendChild(this.remoteAudio)),this.remoteAudio.autoplay=!0,this.remoteAudio.playsInline=!0,this.remoteAudio.volume=.8,this.callButton&&this.callButton.addEventListener(`click`,()=>this.makeCall()),this.hangupButton&&this.hangupButton.addEventListener(`click`,()=>this.hangup()),this.muteButton&&this.muteButton.addEventListener(`click`,()=>this.toggleMute()),this.volumeSlider&&this.volumeSlider.addEventListener(`input`,e=>this.setVolume(e.target.value)),this.setupKeypad(),this.updateUIState(`idle`)}setupKeypad(){let t=document.querySelectorAll(`.keypad-btn`);t.forEach(e=>{e.addEventListener(`click`,e=>{let t=e.currentTarget.getAttribute(`data-digit`);t&&this.isCallActive&&(this.playDTMFTone(t),this.sendDTMF(t),e.currentTarget.classList.add(`pressed`),setTimeout(()=>{e.currentTarget.classList.remove(`pressed`)},300))})}),e(`Keypad initialized with`,t.length,`buttons`)}updateStatus(t,n=`info`){this.statusDiv&&(this.statusDiv.textContent=t,this.statusDiv.className=`webrtc-status ${n}`,debugLog(`[WebRTC Phone] ${t}`),e(`Status update:`,{message:t,type:n}))}updateUIState(e){this.callButton&&(this.callButton.disabled=e!==`idle`),this.hangupButton&&(this.hangupButton.disabled=e===`idle`),this.muteButton&&(this.muteButton.disabled=e===`idle`);let t=e===`connected`||e===`calling`;this.keypadSection&&(this.keypadSection.style.display=t?`block`:`none`),document.querySelectorAll(`.keypad-btn`).forEach(e=>{e.disabled=!t})}_checkWebRTCSupport(){if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){let e=`WebRTC not supported in this browser or context. Try using HTTPS or a modern browser.`;return this.updateStatus(e,`error`),console.error(`[WebRTC Phone]`,e),!1}return!0}async requestMicrophoneAccess(){try{return this.updateStatus(`Requesting microphone access...`,`info`),this._checkWebRTCSupport()?((await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:!0,noiseSuppression:!0,autoGainControl:!0},video:!1})).getTracks().forEach(e=>e.stop()),this.updateStatus(`Ready to call (microphone access granted)`,`success`),debugLog(`[WebRTC Phone] Microphone access granted`),!0):!1}catch(e){return console.error(`[WebRTC Phone] Microphone access denied:`,e),this.updateStatus(`Microphone access denied. Please allow microphone access in browser settings.`,`error`),!1}}async createSession(){try{this.updateStatus(`Creating WebRTC session...`,`info`),e(`Creating WebRTC session:`,{url:`${this.apiUrl}/api/webrtc/session`,extension:this.extension});let t=await fetch(`${this.apiUrl}/api/webrtc/session`,{method:`POST`,headers:this.getAuthHeaders(),body:JSON.stringify({extension:this.extension})});if(e(`Session creation response:`,{status:t.status,statusText:t.statusText,ok:t.ok}),!t.ok){let n=await t.text();throw e(`Session creation failed - response text:`,n),Error(`Failed to create session: ${t.statusText} - ${n}`)}let n=await t.json();if(e(`Session creation response data:`,n),!n.success)throw Error(`Session creation failed`);return this.sessionId=n.session.session_id,this.updateStatus(`Session created: ${this.sessionId}`,`success`),e(`Session created successfully:`,{sessionId:this.sessionId,iceServers:n.ice_servers}),this.peerConnection=new RTCPeerConnection(n.ice_servers),e(`RTCPeerConnection created with configuration:`,n.ice_servers),this.peerConnection.onicecandidate=t=>{e(`ICE candidate event:`,{candidate:t.candidate,url:t.url}),t.candidate&&this.sendICECandidate(t.candidate)},this.peerConnection.onconnectionstatechange=()=>{debugLog(`Connection state: ${this.peerConnection.connectionState}`),e(`Connection state changed:`,{connectionState:this.peerConnection.connectionState,iceConnectionState:this.peerConnection.iceConnectionState,iceGatheringState:this.peerConnection.iceGatheringState,signalingState:this.peerConnection.signalingState}),this.peerConnection.connectionState===`connected`?e(`WebRTC media path ready (waiting for SIP call progress)`):this.peerConnection.connectionState===`failed`?(e(`Connection FAILED - checking stats...`),this.stopRingbackTone(),this.updateStatus(`Connection failed`,`error`),this.hangup()):this.peerConnection.connectionState===`disconnected`&&(e(`Connection DISCONNECTED`),this.stopRingbackTone(),this.updateStatus(`Call disconnected`,`warning`),this.hangup())},this.peerConnection.oniceconnectionstatechange=()=>{debugLog(`ICE connection state: ${this.peerConnection.iceConnectionState}`),e(`ICE connection state changed:`,{iceConnectionState:this.peerConnection.iceConnectionState,iceGatheringState:this.peerConnection.iceGatheringState}),this.peerConnection.iceConnectionState===`failed`&&e(`ICE connection FAILED - this usually means network connectivity issues`)},this.peerConnection.ontrack=t=>{if(debugLog(`Received remote track`),e(`Remote track received:`,{track:t.track,streams:t.streams,trackKind:t.track.kind,trackId:t.track.id}),this.remoteAudio&&t.streams?.length>0){this.remoteAudio.srcObject=t.streams[0];let e=this.remoteAudio.play();e!==void 0&&e.then(()=>{debugLog(`Remote audio playback started successfully`),this.updateStatus(`Audio connected`,`success`)}).catch(e=>{debugWarn(`Audio autoplay prevented:`,e),this.updateStatus(`Audio ready (click to unmute if needed)`,`warning`)})}else debugWarn(`No remote audio element or streams available`)},!0}catch(t){return console.error(`Error creating session:`,t),e(`Error in createSession:`,{error:t,message:t.message,stack:t.stack}),this.updateStatus(`Error: ${t.message}`,`error`),!1}}async makeCall(){try{let t=this.targetExtension?.value??`1001`;if(e(`makeCall() called:`,{targetExtension:t}),!t){this.updateStatus(`Please enter target extension`,`error`);return}if(this.updateUIState(`connecting`),this.updateStatus(`Requesting microphone access...`,`info`),!this._checkWebRTCSupport()){this.updateUIState(`idle`);return}try{e(`Requesting user media...`),this.localStream=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:!0,noiseSuppression:!0,autoGainControl:!0},video:!1}),e(`User media granted:`,{streamId:this.localStream.id,audioTracks:this.localStream.getAudioTracks().map(e=>({id:e.id,kind:e.kind,label:e.label,enabled:e.enabled,muted:e.muted,readyState:e.readyState}))}),this.updateStatus(`Microphone access granted`,`success`)}catch(t){console.error(`Microphone access denied:`,t),e(`Microphone access error:`,{error:t,name:t.name,message:t.message}),this.updateStatus(`Microphone access denied. Please allow microphone access.`,`error`),this.updateUIState(`idle`);return}if(e(`Creating WebRTC session...`),!await this.createSession()){e(`Session creation failed`),this.updateUIState(`idle`);return}e(`Adding local tracks to peer connection...`);for(let t of this.localStream.getTracks())this.peerConnection.addTrack(t,this.localStream),debugLog(`Added local track to peer connection`),e(`Track added:`,{trackId:t.id,kind:t.kind,label:t.label});this.updateStatus(`Creating call offer...`,`info`),e(`Creating RTC offer...`);let n=await this.peerConnection.createOffer();e(`Offer created:`,{type:n.type,sdpLength:n.sdp.length}),await this.peerConnection.setLocalDescription(n),e(`Local description set`),debugLog(`Waiting for ICE gathering to complete...`),await new Promise(e=>{if(this.peerConnection.iceGatheringState===`complete`){e();return}let t=()=>{this.peerConnection.iceGatheringState===`complete`&&(this.peerConnection.removeEventListener(`icegatheringstatechange`,t),e())};this.peerConnection.addEventListener(`icegatheringstatechange`,t),setTimeout(()=>{this.peerConnection.removeEventListener(`icegatheringstatechange`,t),debugLog(`ICE gathering timed out after 5 s, proceeding with available candidates`),e()},5e3)});let r=this.peerConnection.localDescription;debugLog(`SDP Offer created (ICE gathering done):`,r.sdp),e(`Full SDP Offer:`,r.sdp),e(`Sending offer to PBX:`,{url:`${this.apiUrl}/api/webrtc/offer`,sessionId:this.sessionId});let i=await fetch(`${this.apiUrl}/api/webrtc/offer`,{method:`POST`,headers:this.getAuthHeaders(),body:JSON.stringify({session_id:this.sessionId,sdp:r.sdp})});if(e(`Offer response:`,{status:i.status,statusText:i.statusText,ok:i.ok}),!i.ok){let t=await i.text();throw e(`Offer failed - response text:`,t),Error(`Failed to send offer: ${i.statusText} - ${t}`)}let a=await i.json();if(e(`Offer response data:`,a),!a.success)throw Error(`Offer was rejected by server`);if(a.answer_sdp){e(`Setting remote description from server SDP answer`);let t=new RTCSessionDescription({type:`answer`,sdp:a.answer_sdp});await this.peerConnection.setRemoteDescription(t),e(`Remote description set successfully`)}else e(`No SDP answer from server (legacy mode)`);this.updateStatus(`Calling extension ${t}...`,`info`),this.updateUIState(`calling`),e(`Initiating call to target extension:`,{url:`${this.apiUrl}/api/webrtc/call`,sessionId:this.sessionId,targetExtension:t});let o=await fetch(`${this.apiUrl}/api/webrtc/call`,{method:`POST`,headers:this.getAuthHeaders(),body:JSON.stringify({session_id:this.sessionId,target_extension:t})});e(`Call response:`,{status:o.status,statusText:o.statusText,ok:o.ok});let s=await o.json();if(e(`Call response data:`,s),s.success)this.isCallActive=!0,this.callId=s.call_id,this.updateStatus(`Calling ${t}...`,`info`),e(`Call initiated successfully:`,{callId:s.call_id}),this.startRingbackTone(),this._startCallStatusPolling();else throw e(`Call initiation failed:`,s),Error(s.error??`Failed to initiate call`)}catch(t){console.error(`Error making call:`,t),e(`Error in makeCall:`,{error:t,message:t.message,stack:t.stack}),this.updateStatus(`Call failed: ${t.message}`,`error`),this.hangup()}}async sendICECandidate(t){try{if(!this.sessionId){debugWarn(`Cannot send ICE candidate: no active session`),e(`ICE candidate send skipped - no session`);return}e(`Sending ICE candidate:`,{sessionId:this.sessionId,candidate:t.candidate,sdpMid:t.sdpMid,sdpMLineIndex:t.sdpMLineIndex}),await fetch(`${this.apiUrl}/api/webrtc/ice-candidate`,{method:`POST`,headers:this.getAuthHeaders(),body:JSON.stringify({session_id:this.sessionId,candidate:{candidate:t.candidate,sdpMid:t.sdpMid,sdpMLineIndex:t.sdpMLineIndex}})}),debugLog(`ICE candidate sent`),e(`ICE candidate sent successfully`)}catch(t){console.error(`Error sending ICE candidate:`,t),e(`Error sending ICE candidate:`,{error:t,message:t.message})}}async sendDTMF(t){if(!this.isCallActive||!this.sessionId){debugWarn(`Cannot send DTMF: No active call`);return}try{e(`Sending DTMF digit:`,t);let n=await fetch(`${this.apiUrl}/api/webrtc/dtmf`,{method:`POST`,headers:this.getAuthHeaders(),body:JSON.stringify({session_id:this.sessionId,digit:t,duration:160})});if(e(`DTMF response:`,{status:n.status,statusText:n.statusText}),n.ok){let r=await n.json();debugLog(`DTMF '${t}' sent successfully`),e(`DTMF send result:`,r);let i=this.statusDiv?.textContent??``;this.updateStatus(`Sent: ${t}`,`info`),setTimeout(()=>{this.isCallActive&&this.updateStatus(i,`success`)},500)}else console.error(`Failed to send DTMF '${t}':`,n.statusText),this.updateStatus(`Failed to send: ${t}`,`error`)}catch(t){console.error(`Error sending DTMF:`,t),e(`DTMF send error:`,{error:t,message:t.message})}}async hangup(){if(debugLog(`Hanging up call`),this.stopRingbackTone(),this._stopCallStatusPolling(),this.sessionId||this.callId)try{e(`Notifying server of hangup:`,{sessionId:this.sessionId,callId:this.callId});let t=await fetch(`${this.apiUrl}/api/webrtc/hangup`,{method:`POST`,headers:this.getAuthHeaders(),body:JSON.stringify({session_id:this.sessionId,call_id:this.callId})});t.ok?(e(`Server hangup response:`,await t.json()),debugLog(`Server notified of call termination`)):(debugWarn(`Failed to notify server of hangup:`,t.statusText),e(`Hangup notification failed:`,{status:t.status,statusText:t.statusText}))}catch(t){console.error(`Error notifying server of hangup:`,t),e(`Error in hangup notification:`,{error:t,message:t.message})}if(this.localStream){for(let e of this.localStream.getTracks())e.stop();this.localStream=null}this.peerConnection&&=(this.peerConnection.close(),null),this.sessionId=null,this.callId=null,this.isCallActive=!1,this.updateStatus(`Call ended`,`info`),this.updateUIState(`idle`)}toggleMute(){if(!this.localStream)return;let e=this.localStream.getAudioTracks()[0];e&&(e.enabled=!e.enabled,this.muteButton&&(this.muteButton.textContent=e.enabled?`🔇 Mute`:`🔊 Unmute`,this.muteButton.classList.toggle(`muted`,!e.enabled)),this.updateStatus(e.enabled?`Unmuted`:`Muted`,`info`))}setVolume(e){this.remoteAudio&&(this.remoteAudio.volume=e/100,debugLog(`Volume set to ${e}%`))}_startRingback(){if(!this._ringbackOsc)try{let e=new(window.AudioContext||window.webkitAudioContext),t=e.createGain();t.gain.value=.15;let n=e.createOscillator();n.frequency.value=440;let r=e.createOscillator();r.frequency.value=480,n.connect(t),r.connect(t),t.connect(e.destination),n.start(),r.start(),this._ringbackCtx=e,this._ringbackOsc=[n,r],this._ringbackGain=t;let i=!0;this._ringbackTimer=setInterval(()=>{i=!i,t.gain.setValueAtTime(i?.15:0,e.currentTime)},i?2e3:4e3),clearInterval(this._ringbackTimer);let a=()=>{this._ringbackCtx&&(t.gain.setValueAtTime(.15,e.currentTime),t.gain.setValueAtTime(0,e.currentTime+2),this._ringbackTimer=setTimeout(a,6e3))};a(),debugLog(`Ringback tone started`)}catch(e){debugWarn(`Could not start ringback tone:`,e)}}_stopRingback(){if(this._ringbackTimer&&=(clearTimeout(this._ringbackTimer),null),this._ringbackOsc&&=(this._ringbackOsc.forEach(e=>{try{e.stop()}catch{}}),null),this._ringbackCtx){try{this._ringbackCtx.close()}catch{}this._ringbackCtx=null}this._ringbackGain=null}_startStatusPolling(){this._statusPollTimer||(this._statusPollTimer=setInterval(()=>this._pollCallStatus(),1e3),e(`Call status polling started`))}_stopStatusPolling(){this._statusPollTimer&&(clearInterval(this._statusPollTimer),this._statusPollTimer=null,e(`Call status polling stopped`))}async _pollCallStatus(){if(this.sessionId)try{let t=await fetch(`${this.apiUrl}/api/webrtc/call-status`,{method:`POST`,headers:this.getAuthHeaders(),body:JSON.stringify({session_id:this.sessionId,call_id:this.callId})});if(!t.ok)return;let n=await t.json();e(`Call status poll:`,n),n.status===`ringing`?(this._startRingback(),this.updateStatus(`Ringing...`,`info`)):n.status===`connected`?(this._stopRingback(),this.updateStatus(`Call connected`,`success`),this.updateUIState(`connected`)):n.status===`ended`&&(this._stopRingback(),this._stopStatusPolling(),this.updateStatus(`Call ended by remote`,`info`),this.hangup())}catch{}}},n=null,r=`webrtc-admin`;async function i(){let e=window.location.origin,i=r;try{let e=localStorage.getItem(`pbx_token`),t=e?{Authorization:`Bearer ${e}`}:{},n=await(await fetch(`/api/webrtc/phone-config`,{headers:t})).json();n.success&&n.extension?(i=n.extension,debugLog(`WebRTC Phone using configured extension:`,i)):debugLog(`WebRTC Phone using default extension:`,i)}catch(e){debugWarn(`Failed to load WebRTC phone config, using default:`,e)}n&&n.isCallActive&&await n.hangup(),n=new t(e,i),debugLog(`WebRTC Phone initialized with extension:`,i),await n.requestMicrophoneAccess();let a=document.getElementById(`webrtc-status`);a&&(i===r?a.textContent=`Ready to call (Default extension)`:a.textContent=`Ready to call (Extension: ${i})`)}document.readyState===`loading`?document.addEventListener(`DOMContentLoaded`,i):i();function a(){return`
        <h2>🎯 Framework Features Overview</h2>
        <div class="info-box" style="background: #e3f2fd; border-left: 4px solid #2196f3; padding: 15px; margin-bottom: 20px;">
            <p><strong>100% Free & Open Source</strong> - All framework features use only free and open-source technologies. No paid services required!</p>
            <p style="margin-top: 10px;"><strong>Implementation Status Legend:</strong></p>
            <p style="margin: 5px 0;"><span class="status-badge status-fully-implemented">✅ Fully Implemented</span> = Production-ready with complete admin UI</p>
            <p style="margin: 5px 0;"><span class="status-badge status-enhanced">🔧 Enhanced Admin UI</span> = Full UI with live data, needs external service integration</p>
            <p style="margin: 5px 0;"><span class="status-badge status-framework-only">⚙️ Framework Only</span> = Backend ready, basic UI, needs service integration</p>
        </div>

        <h3 style="margin-top: 30px;">✅ Fully Implemented Features (Production-Ready)</h3>
        <div class="stats-grid">
            <div class="stat-card" style="background: #e8f5e9; border-left: 4px solid #4caf50;">
                <div class="stat-icon">📲</div>
                <h3>Click-to-Dial</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-fully-implemented">✅ Fully Implemented</span>
                </div>
                <p>Web-based dialing with full PBX integration</p>
                <small style="color: #2e7d32; display: block; margin-top: 8px;">✓ SIP call creation ✓ Auto-answer ✓ Call history ✓ REST API</small>
                <button onclick="switchTab('click-to-dial')" class="btn-success" style="margin-top: 10px;">Use Now</button>
            </div>
            <div class="stat-card" style="background: #e8f5e9; border-left: 4px solid #4caf50;">
                <div class="stat-icon">📢</div>
                <h3>Paging System</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-fully-implemented">✅ Fully Implemented</span>
                </div>
                <p>Overhead paging with zone management</p>
                <small style="color: #2e7d32; display: block; margin-top: 8px;">✓ Zone configuration ✓ DAC management ✓ Active monitoring ✓ Full REST API</small>
                <button onclick="switchTab('paging')" class="btn-success" style="margin-top: 10px;">Use Now</button>
            </div>
            <div class="stat-card" style="background: #e8f5e9; border-left: 4px solid #4caf50;">
                <div class="stat-icon">🎙️</div>
                <h3>Speech Analytics</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-fully-implemented">✅ Fully Implemented</span>
                </div>
                <p>Real-time transcription and sentiment analysis (FREE: Vosk offline)</p>
                <small style="color: #2e7d32; display: block; margin-top: 8px;">✓ Live transcription ✓ Sentiment analysis ✓ Call summaries ✓ No cloud costs</small>
                <button onclick="switchTab('speech-analytics')" class="btn-success" style="margin-top: 10px;">Use Now</button>
            </div>
            <div class="stat-card" style="background: #e8f5e9; border-left: 4px solid #4caf50;">
                <div class="stat-icon">📍</div>
                <h3>Nomadic E911</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-fully-implemented">✅ Fully Implemented</span>
                </div>
                <p>Location-based emergency routing for remote workers</p>
                <small style="color: #2e7d32; display: block; margin-top: 8px;">✓ IP tracking ✓ Multi-site support ✓ Location history ✓ REST API</small>
                <button onclick="switchTab('nomadic-e911')" class="btn-success" style="margin-top: 10px;">Use Now</button>
            </div>
        </div>

        <h3 style="margin-top: 30px;">🔧 Enhanced Admin UI Features (Live Data Integration)</h3>
        <div class="stats-grid">
            <div class="stat-card" style="background: #fff3e0; border-left: 4px solid #ff9800;">
                <div class="stat-icon">🤖</div>
                <h3>Conversational AI</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-enhanced">🔧 Enhanced Admin UI</span>
                </div>
                <p>AI assistant with live statistics (FREE: Rasa, ChatterBot)</p>
                <small style="color: #e65100; display: block; margin-top: 8px;">✓ Full UI ✓ Live statistics ✓ API integration ⚠ Needs AI service (free options available)</small>
                <button onclick="switchTab('conversational-ai')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card" style="background: #fff3e0; border-left: 4px solid #ff9800;">
                <div class="stat-icon">📞</div>
                <h3>Predictive Dialing</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-enhanced">🔧 Enhanced Admin UI</span>
                </div>
                <p>Campaign management with live statistics (FREE: Vicidial)</p>
                <small style="color: #e65100; display: block; margin-top: 8px;">✓ Full UI ✓ Campaign tracking ✓ Statistics dashboard ⚠ Needs dialer engine (free options available)</small>
                <button onclick="switchTab('predictive-dialing')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card" style="background: #fff3e0; border-left: 4px solid #ff9800;">
                <div class="stat-icon">🔊</div>
                <h3>Voice Biometrics</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-enhanced">🔧 Enhanced Admin UI</span>
                </div>
                <p>Speaker authentication with enrollment tracking (FREE: speaker-recognition)</p>
                <small style="color: #e65100; display: block; margin-top: 8px;">✓ Full UI ✓ Profile management ✓ Verification tracking ⚠ Needs biometric engine (free options available)</small>
                <button onclick="switchTab('voice-biometrics')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card" style="background: #fff3e0; border-left: 4px solid #ff9800;">
                <div class="stat-icon">📈</div>
                <h3>BI Integration</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-enhanced">🔧 Enhanced Admin UI</span>
                </div>
                <p>Dataset browser with export (FREE: Metabase, Superset, Redash)</p>
                <small style="color: #e65100; display: block; margin-top: 8px;">✓ Full UI ✓ Export functionality ✓ Multiple formats ⚠ Needs BI tool (free options available)</small>
                <button onclick="switchTab('bi-integration')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card" style="background: #fff3e0; border-left: 4px solid #ff9800;">
                <div class="stat-icon">🏷️</div>
                <h3>Call Tagging</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-enhanced">🔧 Enhanced Admin UI</span>
                </div>
                <p>Tag management with analytics (FREE: spaCy NLP)</p>
                <small style="color: #e65100; display: block; margin-top: 8px;">✓ Full UI ✓ Tag management ✓ Live statistics ⚠ Needs AI classifier (free options available)</small>
                <button onclick="switchTab('call-tagging')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card" style="background: #fff3e0; border-left: 4px solid #ff9800;">
                <div class="stat-icon">📱</div>
                <h3>Mobile Apps</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-enhanced">🔧 Enhanced Admin UI</span>
                </div>
                <p>Device management with statistics (FREE: React Native + WebRTC)</p>
                <small style="color: #e65100; display: block; margin-top: 8px;">✓ Full UI ✓ Device tracking ✓ Push config ⚠ Needs native app development (free frameworks available)</small>
                <button onclick="switchTab('mobile-push')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
        </div>

        <h3 style="margin-top: 30px;">⚙️ Framework Features (Backend Ready)</h3>
        <div class="stats-grid">
            <div class="stat-card">
                <div class="stat-icon">📊</div>
                <h3>Call Quality Prediction</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                </div>
                <p>ML-based QoS prediction (FREE: scikit-learn)</p>
                <small style="color: #666; display: block; margin-top: 8px;">✓ Metrics tracking ✓ Alerting ⚠ Needs ML model (free framework available)</small>
                <button onclick="switchTab('call-quality-prediction')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>

            <div class="stat-card">
                <div class="stat-icon">🎬</div>
                <h3>Video Codecs (H.264/H.265)</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                </div>
                <p>Video codec support (FREE: FFmpeg, OpenH264)</p>
                <small style="color: #666; display: block; margin-top: 8px;">✓ Codec negotiation ✓ Bandwidth calc ⚠ Needs FFmpeg/OpenH264 (free)</small>
                <button onclick="switchTab('video-codec')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🔄</div>
                <h3>Number Portability</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                </div>
                <p>Use business number on mobile device</p>
                <small style="color: #666; display: block; margin-top: 8px;">✓ DID mapping ✓ Simultaneous ring ⚠ Needs mobile integration</small>
                <button onclick="switchTab('mobile-number-portability')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🎙️</div>
                <h3>Recording Analytics</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                </div>
                <p>AI analysis of recorded calls (FREE: Vosk + spaCy)</p>
                <small style="color: #666; display: block; margin-top: 8px;">✓ Sentiment ✓ Keywords ⚠ Needs NLP service (free options available)</small>
                <button onclick="switchTab('recording-analytics')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🔀</div>
                <h3>Call Blending</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                </div>
                <p>Mix inbound/outbound calls for efficiency</p>
                <small style="color: #666; display: block; margin-top: 8px;">✓ Mode switching ✓ Priority distribution ⚠ Needs queue integration</small>
                <button onclick="switchTab('call-blending')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card">
                <div class="stat-icon">📭</div>
                <h3>Voicemail Drop</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                </div>
                <p>Auto-leave message on voicemail detection (FREE: pyAudioAnalysis)</p>
                <small style="color: #666; display: block; margin-top: 8px;">✓ AMD ✓ Message library ⚠ Needs detection algorithm (free options available)</small>
                <button onclick="switchTab('voicemail-drop')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🌍</div>
                <h3>Geographic Redundancy</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                </div>
                <p>Multi-region trunk registration with failover</p>
                <small style="color: #666; display: block; margin-top: 8px;">✓ Region management ✓ Health monitoring ⚠ Needs multi-region setup</small>
                <button onclick="switchTab('geo-redundancy')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🌐</div>
                <h3>DNS SRV Failover</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                </div>
                <p>Automatic server failover using DNS SRV (FREE: BIND, PowerDNS)</p>
                <small style="color: #666; display: block; margin-top: 8px;">✓ Priority selection ✓ Load balancing ⚠ Needs DNS SRV records (free DNS servers available)</small>
                <button onclick="switchTab('dns-srv-failover')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🛡️</div>
                <h3>Session Border Controller</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                </div>
                <p>Enhanced security and NAT traversal (FREE: Kamailio, OpenSIPS)</p>
                <small style="color: #666; display: block; margin-top: 8px;">✓ Topology hiding ✓ Security filtering ⚠ Needs SBC deployment (free options available)</small>
                <button onclick="switchTab('session-border-controller')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card">
                <div class="stat-icon">🗺️</div>
                <h3>Data Residency Controls</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                </div>
                <p>Geographic data storage options for compliance</p>
                <small style="color: #666; display: block; margin-top: 8px;">✓ Region management ✓ GDPR support ⚠ Needs multi-region storage</small>
                <button onclick="switchTab('data-residency')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card">
                <div class="stat-icon">📹</div>
                <h3>Video Conferencing</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                </div>
                <p>HD/4K video calls with screen sharing (FREE: Jitsi, BigBlueButton)</p>
                <small style="color: #666; display: block; margin-top: 8px;">✓ Room management ✓ Participant tracking ⚠ Needs video service (free options available)</small>
                <button onclick="switchTab('video-conferencing')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
            <div class="stat-card">
                <div class="stat-icon">💬</div>
                <h3>Team Messaging</h3>
                <div style="margin: 10px 0;">
                    <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                </div>
                <p>Slack/Teams alternative with channels and file sharing (FREE: Matrix, Rocket.Chat)</p>
                <small style="color: #666; display: block; margin-top: 8px;">✓ Channel management ✓ Message tracking ⚠ Needs messaging server (free options available)</small>
                <button onclick="switchTab('team-messaging')" class="btn-primary" style="margin-top: 10px;">Configure</button>
            </div>
        </div>

        <div class="section-card" style="margin-top: 30px; background: #e8f5e9; border-left: 4px solid #4caf50;">
            <h3>💚 100% Free & Open Source</h3>
            <div class="info-box" style="background: white;">
                <p><strong>All framework features can be implemented using only free and open-source technologies:</strong></p>
                <ul style="margin-top: 10px;">
                    <li>✅ <strong>Vosk:</strong> FREE offline speech recognition (instead of Google/AWS)</li>
                    <li>✅ <strong>spaCy & NLTK:</strong> FREE NLP and AI classification (instead of OpenAI/Azure)</li>
                    <li>✅ <strong>scikit-learn:</strong> FREE machine learning framework</li>
                    <li>✅ <strong>Metabase/Superset/Redash:</strong> FREE business intelligence tools</li>
                    <li>✅ <strong>React Native:</strong> FREE mobile app framework</li>
                    <li>✅ <strong>Rasa/ChatterBot:</strong> FREE conversational AI frameworks</li>
                    <li>✅ <strong>Vicidial:</strong> FREE predictive dialer (open source)</li>
                    <li>✅ <strong>FFmpeg:</strong> FREE audio/video processing</li>
                    <li>✅ <strong>Kamailio/OpenSIPS:</strong> FREE SIP servers for SBC</li>
                </ul>
                <p style="margin-top: 15px; font-weight: bold; color: #2e7d32;">💰 Total Cost: $0 - No licensing fees, no cloud costs, no subscriptions!</p>
            </div>
        </div>

        <div class="section-card" style="margin-top: 20px;">
            <h3>📋 Implementation Notes</h3>
            <div class="info-box">
                <p>All framework features include:</p>
                <ul>
                    <li>✅ <strong>Database Schemas:</strong> Tables and relationships defined and ready</li>
                    <li>✅ <strong>REST APIs:</strong> Endpoints for configuration and management</li>
                    <li>✅ <strong>Logging:</strong> Comprehensive logging infrastructure</li>
                    <li>✅ <strong>Configuration:</strong> Enable/disable flags and settings</li>
                    <li>✅ <strong>Free Integration Options:</strong> All features have documented free/open-source integration options</li>
                </ul>
                <p style="margin-top: 15px;"><strong>Total Lines of Code:</strong> ~5,200 lines across 16 frameworks</p>
                <p><strong>Tests:</strong> All frameworks have comprehensive test coverage with 100% pass rate</p>
                <p><strong>Documentation:</strong> Each feature has detailed implementation guides</p>
            </div>
        </div>

    `}function o(){return setTimeout(async()=>{try{s((await(await fetch(`/api/framework/click-to-dial/configs`,{headers:pbxAuthHeaders()})).json()).configs??[])}catch(e){let t=document.getElementById(`click-to-dial-configs-list`);t&&(t.innerHTML=`<div class="error-box">Error loading configurations: ${escapeHtml(e.message)}</div>`)}},100),`
        <h2>📲 Click-to-Dial Configuration</h2>
        <div class="info-box" style="background: #e8f5e9; border-left: 4px solid #4caf50;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-fully-implemented">✅ Fully Implemented</span>
                <strong>Production Ready</strong>
            </div>
            <p>Click-to-Dial is fully implemented with PBX integration. Users can initiate calls from web browsers, CRM systems, or mobile apps.</p>
            <p><strong>Features:</strong> Auto-answer, browser notifications, call history tracking, WebRTC integration</p>
        </div>

        <div class="section-card">
            <h3>Extension Configurations</h3>
            <div id="click-to-dial-configs-list">Loading...</div>
        </div>

        <div class="section-card">
            <h3>Call History</h3>
            <div id="click-to-dial-history">
                <p>Select an extension to view call history</p>
            </div>
        </div>
    `}function s(e){let t=document.getElementById(`click-to-dial-configs-list`);if(!t){debugWarn(`Click-to-dial configs container not found`);return}if(e.length===0){t.innerHTML=`<p>No configurations found. Configurations are created automatically when extensions use click-to-dial.</p>`;return}t.innerHTML=`
        <table class="data-table">
            <thead>
                <tr>
                    <th>Extension</th>
                    <th>Enabled</th>
                    <th>Auto Answer</th>
                    <th>Browser Notifications</th>
                    <th>Actions</th>
                </tr>
            </thead>
            <tbody>
                ${e.map(e=>`
                    <tr>
                        <td>${escapeHtml(String(e.extension))}</td>
                        <td>${e.enabled?`✅ Yes`:`❌ No`}</td>
                        <td>${e.auto_answer?`✅ Yes`:`❌ No`}</td>
                        <td>${e.browser_notification?`✅ Yes`:`❌ No`}</td>
                        <td>
                            <button onclick="viewClickToDialHistory('${escapeHtml(String(e.extension))}')" class="btn-secondary btn-sm">View History</button>
                        </td>
                    </tr>
                `).join(``)}
            </tbody>
        </table>
    `}var c=async e=>{try{let t=(await(await fetch(`/api/framework/click-to-dial/history/${e}`,{headers:pbxAuthHeaders()})).json()).history??[],n=document.getElementById(`click-to-dial-history`);if(t.length===0){n.innerHTML=`<p>No call history for extension ${escapeHtml(String(e))}</p>`;return}n.innerHTML=`
            <h4>Call History for Extension ${escapeHtml(String(e))}</h4>
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Destination</th>
                        <th>Source</th>
                        <th>Status</th>
                        <th>Initiated</th>
                        <th>Connected</th>
                    </tr>
                </thead>
                <tbody>
                    ${t.map(e=>`
                        <tr>
                            <td>${escapeHtml(String(e.destination))}</td>
                            <td>${escapeHtml(String(e.source))}</td>
                            <td><span class="status-badge status-${escapeHtml(String(e.status))}">${escapeHtml(String(e.status))}</span></td>
                            <td>${new Date(e.initiated_at).toLocaleString()}</td>
                            <td>${e.connected_at?new Date(e.connected_at).toLocaleString():`-`}</td>
                        </tr>
                    `).join(``)}
                </tbody>
            </table>
        `}catch(e){document.getElementById(`click-to-dial-history`).innerHTML=`<div class="error-box">Error loading history: ${escapeHtml(e.message)}</div>`}};function l(){return setTimeout(()=>{u()},100),`
        <h2>📹 Video Conferencing</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>Video conferencing framework with support for HD/4K video calls and screen sharing.</p>
            <p><strong>Note:</strong> This framework provides database tracking only. Video conferencing is typically handled by external services like Zoom, Microsoft Teams, or custom WebRTC implementation.</p>
            <p><strong>Available:</strong> Room management, participant tracking, configuration storage</p>
        </div>

        <div class="section-card">
            <h3>Conference Rooms</h3>
            <button onclick="showCreateRoomDialog()" class="btn-primary">+ Create Room</button>
            <div id="video-rooms-list" style="margin-top: 15px;">Loading...</div>
        </div>

        <div id="create-room-dialog" style="display: none;" class="modal-overlay">
            <div class="modal-content">
                <h3>Create Video Conference Room</h3>
                <form id="create-room-form">
                    <div class="form-group">
                        <label>Room Name:</label>
                        <input type="text" name="room_name" required class="form-control">
                    </div>
                    <div class="form-group">
                        <label>Owner Extension:</label>
                        <input type="text" name="owner_extension" class="form-control">
                    </div>
                    <div class="form-group">
                        <label>Max Participants:</label>
                        <input type="number" name="max_participants" value="10" class="form-control">
                    </div>
                    <div class="form-group">
                        <label>
                            <input type="checkbox" name="enable_4k"> Enable 4K Video
                        </label>
                    </div>
                    <div class="form-group">
                        <label>
                            <input type="checkbox" name="enable_screen_share" checked> Enable Screen Sharing
                        </label>
                    </div>
                    <div class="form-group">
                        <label>
                            <input type="checkbox" name="recording_enabled"> Enable Recording
                        </label>
                    </div>
                    <div class="form-actions">
                        <button type="submit" class="btn-primary">Create Room</button>
                        <button type="button" onclick="hideCreateRoomDialog()" class="btn-secondary">Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    `}var u=async()=>{try{ee((await(await fetch(`/api/framework/video-conference/rooms`,{headers:pbxAuthHeaders()})).json()).rooms??[])}catch(e){document.getElementById(`video-rooms-list`).innerHTML=`<div class="error-box">Error loading rooms: ${escapeHtml(e.message)}</div>`}};function ee(e){let t=document.getElementById(`video-rooms-list`);if(e.length===0){t.innerHTML=`<p>No conference rooms created yet.</p>`;return}t.innerHTML=`
        <table class="data-table">
            <thead>
                <tr>
                    <th>Room Name</th>
                    <th>Owner</th>
                    <th>Max Participants</th>
                    <th>4K Enabled</th>
                    <th>Screen Share</th>
                    <th>Recording</th>
                    <th>Created</th>
                </tr>
            </thead>
            <tbody>
                ${e.map(e=>`
                    <tr>
                        <td>${escapeHtml(e.room_name)}</td>
                        <td>${escapeHtml(e.owner_extension||`-`)}</td>
                        <td>${e.max_participants}</td>
                        <td>${e.enable_4k?`✅`:`❌`}</td>
                        <td>${e.enable_screen_share?`✅`:`❌`}</td>
                        <td>${e.recording_enabled?`✅`:`❌`}</td>
                        <td>${new Date(e.created_at).toLocaleDateString()}</td>
                    </tr>
                `).join(``)}
            </tbody>
        </table>
    `}function te(){return`
        <h2>🤖 Conversational AI Assistant</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>Auto-responses and smart call handling using AI technology.</p>
            <p><strong>Supported Services:</strong> OpenAI GPT, Google Dialogflow, Amazon Lex, Microsoft Azure Bot Service</p>
            <p><strong>Features:</strong> Intent detection, entity extraction, conversation context management, auto-responses</p>
        </div>

        <div class="section-card">
            <h3>Configuration</h3>
            <form id="conversational-ai-config-form" style="max-width: 600px;" onsubmit="submitConversationalAIConfig(event)">
                <div class="form-group">
                    <label>
                        <input type="checkbox" id="ai-enabled" name="enabled">
                        Enable Conversational AI
                    </label>
                </div>
                <div class="form-group">
                    <label>AI Provider:</label>
                    <select id="ai-provider" name="provider" class="form-control">
                        <option value="openai">OpenAI GPT</option>
                        <option value="dialogflow">Google Dialogflow</option>
                        <option value="lex">Amazon Lex</option>
                        <option value="azure">Microsoft Azure Bot Service</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>API Key:</label>
                    <input type="password" id="ai-api-key" name="api_key" required class="form-control" placeholder="Enter your API key">
                    <small>API key for the selected provider (stored securely)</small>
                </div>
                <div class="form-group">
                    <label>Model:</label>
                    <input type="text" id="ai-model" name="model" class="form-control" placeholder="gpt-4" value="gpt-4">
                    <small>For OpenAI: gpt-4, gpt-3.5-turbo, etc.</small>
                </div>
                <div class="form-group">
                    <label>Max Tokens:</label>
                    <input type="number" id="ai-max-tokens" name="max_tokens" class="form-control" value="150" min="50" max="4000">
                    <small>Maximum length of AI responses</small>
                </div>
                <div class="form-group">
                    <label>Temperature (0.0 - 1.0):</label>
                    <input type="number" id="ai-temperature" name="temperature" class="form-control" value="0.7" min="0" max="1" step="0.1">
                    <small>Higher = more creative, Lower = more focused</small>
                </div>
                <div class="form-actions">
                    <button type="submit" class="btn-primary">Save Configuration</button>
                    <button type="button" class="btn-secondary" onclick="loadConversationalAIStats()">View Statistics</button>
                </div>
            </form>
        </div>

        <div class="section-card">
            <h3>Statistics</h3>
            <div id="ai-statistics">
                <p>Click "View Statistics" to load current stats</p>
            </div>
        </div>

        <div class="section-card">
            <h3>Integration Requirements</h3>
            <div class="info-box">
                <p><strong>To activate this feature, you need:</strong></p>
                <ul>
                    <li>✅ Conversation context tracking - <strong>Ready</strong></li>
                    <li>✅ Intent and entity detection framework - <strong>Ready</strong></li>
                    <li>✅ Response generation pipeline - <strong>Ready</strong></li>
                    <li>⚠️ AI service API credentials - <strong>Required</strong></li>
                    <li>⚠️ Update config.yml with provider settings</li>
                </ul>
                <p style="margin-top: 15px;"><strong>Example config.yml:</strong></p>
                <pre style="background: #f5f5f5; padding: 10px; border-radius: 4px; overflow-x: auto;">
features:
  conversational_ai:
    enabled: true
    provider: 'openai'
    model: 'gpt-4'
    api_key: 'your-api-key-here'  # Store securely
    max_tokens: 150
    temperature: 0.7</pre>
            </div>
        </div>
    `}var ne=async()=>{let e=document.getElementById(`ai-statistics`);e.innerHTML=`<p>Loading statistics...</p>`;try{let t=(await(await fetch(`/api/framework/conversational-ai/statistics`,{headers:pbxAuthHeaders()})).json()).statistics??{};e.innerHTML=`
            <div class="stats-grid" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">
                <div class="stat-card">
                    <div class="stat-value">${t.total_conversations||0}</div>
                    <div class="stat-label">Total Conversations</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${t.active_conversations||0}</div>
                    <div class="stat-label">Active</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${t.total_messages||0}</div>
                    <div class="stat-label">Messages Processed</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${t.intents_detected||0}</div>
                    <div class="stat-label">Intents Detected</div>
                </div>
            </div>
            ${t.total_conversations===0?`<p style="margin-top: 15px; color: #666;"><em>Note: No conversations yet. Statistics will appear when the feature is enabled and in use.</em></p>`:``}
        `}catch{e.innerHTML=`<p style="color: #666;"><em>Statistics unavailable. The API endpoint (/api/framework/conversational-ai/stats) will be available when the feature is enabled with an AI provider configured.</em></p>`}},re=async e=>{e.preventDefault();let t=e.target,n=new FormData(t),r=n.get(`provider`),i=n.get(`api_key`);if(!i){alert(`API key is required`);return}let a={provider:r,api_key:i,options:{model:n.get(`model`)||`gpt-4`,max_tokens:parseInt(n.get(`max_tokens`))||150,temperature:parseFloat(n.get(`temperature`))||.7}};try{let e=await(await fetch(`/api/framework/conversational-ai/config`,{method:`POST`,headers:pbxAuthHeaders(),body:JSON.stringify(a)})).json();e.success?alert(`Conversational AI configured with ${e.provider} provider.`):alert(`Error saving config: ${e.error??`Unknown error`}`)}catch(e){alert(`Error saving configuration: `+e.message)}};function ie(){return setTimeout(()=>{d(),ae()},100),`
        <h2>📞 Predictive Dialing</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>AI-optimized outbound campaign management with multiple dialing modes.</p>
            <p><strong>Modes:</strong> Preview, Progressive, Predictive, Power</p>
            <p><strong>Features:</strong> Campaign management, contact tracking, agent availability prediction</p>
        </div>

        <div class="section-card">
            <h3>Active Campaigns</h3>
            <button onclick="showCreateCampaignDialog()" class="btn-primary">+ Create Campaign</button>
            <div id="campaigns-list" style="margin-top: 15px;">
                <p>Loading campaigns...</p>
            </div>
        </div>

        <div class="section-card">
            <h3>Campaign Statistics</h3>
            <div id="campaign-statistics">
                <div class="stats-grid" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">
                    <div class="stat-card">
                        <div class="stat-value" id="total-campaigns">0</div>
                        <div class="stat-label">Total Campaigns</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value" id="active-campaigns">0</div>
                        <div class="stat-label">Active</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value" id="calls-today">0</div>
                        <div class="stat-label">Calls Today</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value" id="contact-rate">0%</div>
                        <div class="stat-label">Contact Rate</div>
                    </div>
                </div>
            </div>
        </div>

        <div class="section-card">
            <h3>Dialing Modes</h3>
            <div class="stats-grid" style="grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));">
                <div class="stat-card">
                    <h4>📋 Preview Mode</h4>
                    <p style="color: #666; font-size: 14px;">Agent reviews contact before dialing</p>
                </div>
                <div class="stat-card">
                    <h4>➡️ Progressive Mode</h4>
                    <p style="color: #666; font-size: 14px;">Auto-dial when agent available</p>
                </div>
                <div class="stat-card">
                    <h4>🤖 Predictive Mode</h4>
                    <p style="color: #666; font-size: 14px;">AI predicts agent availability</p>
                </div>
                <div class="stat-card">
                    <h4>⚡ Power Mode</h4>
                    <p style="color: #666; font-size: 14px;">Multiple dials per agent</p>
                </div>
            </div>
        </div>

        <div class="section-card">
            <h3>Integration Requirements</h3>
            <div class="info-box">
                <p><strong>Ready for Integration:</strong></p>
                <ul>
                    <li>✅ Campaign creation and management - <strong>Ready</strong></li>
                    <li>✅ Contact list handling - <strong>Ready</strong></li>
                    <li>✅ Dialing mode configuration - <strong>Ready</strong></li>
                    <li>✅ Agent availability tracking - <strong>Ready</strong></li>
                    <li>⚠️ Dialer engine integration - <strong>Required</strong></li>
                    <li>⚠️ AI agent prediction model - <strong>Optional</strong></li>
                </ul>
            </div>
        </div>
    `}var d=async()=>{try{let e=(await(await fetch(`/api/framework/predictive-dialing/campaigns`,{headers:pbxAuthHeaders()})).json()).campaigns??[],t=document.getElementById(`campaigns-list`);if(e.length===0){t.innerHTML=`<p style="color: #666;">No campaigns created yet. Framework ready for campaign management.</p>`;return}t.innerHTML=`
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Campaign Name</th>
                        <th>Mode</th>
                        <th>Status</th>
                        <th>Contacts</th>
                        <th>Dialed</th>
                        <th>Connected</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${e.map(e=>`
                        <tr>
                            <td><strong>${escapeHtml(e.name)}</strong></td>
                            <td>${escapeHtml(e.mode)}</td>
                            <td><span class="status-badge status-${escapeHtml(e.status)}">${escapeHtml(e.status)}</span></td>
                            <td>${e.total_contacts||0}</td>
                            <td>${e.dialed||0}</td>
                            <td>${e.connected||0}</td>
                            <td>
                                <button onclick="toggleCampaign('${escapeHtml(String(e.id))}')" class="btn-secondary btn-sm">
                                    ${e.status===`active`?`Pause`:`Start`}
                                </button>
                            </td>
                        </tr>
                    `).join(``)}
                </tbody>
            </table>
        `}catch{document.getElementById(`campaigns-list`).innerHTML=`<p style="color: #666;">Framework ready. Campaigns will appear when feature is enabled.</p>`}},ae=async()=>{try{let e=(await(await fetch(`/api/framework/predictive-dialing/statistics`,{headers:pbxAuthHeaders()})).json()).statistics??{};document.getElementById(`total-campaigns`).textContent=e.total_campaigns||0,document.getElementById(`active-campaigns`).textContent=e.active_campaigns||0,document.getElementById(`calls-today`).textContent=e.calls_today||0,document.getElementById(`contact-rate`).textContent=`${e.contact_rate??0}%`}catch{}};function oe(){let e=document.getElementById(`create-campaign-dialog`);e&&e.remove()}function se(){oe(),document.body.insertAdjacentHTML(`beforeend`,`
        <div id="create-campaign-dialog" class="modal" style="display: flex; align-items: center;
             justify-content: center;">
            <div class="modal-content" style="max-width: 600px;">
                <div class="modal-header">
                    <h3>Create Campaign</h3>
                    <span class="close" onclick="hideCreateCampaignDialog()">&times;</span>
                </div>
                <form id="create-campaign-form">
                    <div class="form-group">
                        <label>Campaign ID:</label>
                        <input type="text" name="campaign_id" required class="form-control"
                            placeholder="campaign-001">
                    </div>
                    <div class="form-group">
                        <label>Campaign Name:</label>
                        <input type="text" name="name" required class="form-control"
                            placeholder="Outbound Sales Q1">
                    </div>
                    <div class="form-group">
                        <label>Dialing Mode:</label>
                        <select name="dialing_mode" class="form-control">
                            <option value="progressive">Progressive</option>
                            <option value="preview">Preview</option>
                            <option value="predictive">Predictive</option>
                            <option value="power">Power</option>
                        </select>
                    </div>
                    <div class="modal-actions">
                        <button type="submit" class="btn-primary">Create Campaign</button>
                        <button type="button" class="btn-secondary"
                            onclick="hideCreateCampaignDialog()">Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    `),document.getElementById(`create-campaign-form`).onsubmit=async function(e){e.preventDefault();let t=new FormData(e.target),n={campaign_id:t.get(`campaign_id`),name:t.get(`name`),dialing_mode:t.get(`dialing_mode`)};try{let e=await(await fetch(`/api/framework/predictive-dialing/campaign`,{method:`POST`,headers:pbxAuthHeaders(),body:JSON.stringify(n)})).json();e.success?(alert(`Campaign created successfully!`),oe(),d()):alert(`Error creating campaign: ${e.error??`Unknown error`}`)}catch(e){alert(`Error creating campaign: `+e.message)}}}var ce=async e=>{try{await fetch(`/api/framework/predictive-dialing/campaigns/${e}/toggle`,{method:`POST`,headers:pbxAuthHeaders()}),await d()}catch(e){alert(`Error: ${e.message}`)}};function le(){return setTimeout(()=>{f(),ue()},100),`
        <h2>🔊 Voice Biometrics</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>Speaker authentication and fraud detection using voice biometrics.</p>
            <p><strong>Supported Services:</strong> Nuance, Pindrop, AWS Connect Voice ID</p>
            <p><strong>Features:</strong> Voice enrollment, verification, fraud detection</p>
        </div>

        <div class="section-card">
            <h3>Enrolled Users</h3>
            <button onclick="showEnrollUserDialog()" class="btn-primary">+ Enroll User</button>
            <div id="biometric-profiles-list" style="margin-top: 15px;">
                <p>Loading voice profiles...</p>
            </div>
        </div>

        <div class="section-card">
            <h3>Statistics</h3>
            <div id="biometric-statistics">
                <div class="stats-grid" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">
                    <div class="stat-card">
                        <div class="stat-value" id="enrolled-users">0</div>
                        <div class="stat-label">Enrolled Users</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value" id="verifications-today">0</div>
                        <div class="stat-label">Verifications Today</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value" id="success-rate">0%</div>
                        <div class="stat-label">Success Rate</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-value" id="fraud-attempts">0</div>
                        <div class="stat-label">Fraud Attempts</div>
                    </div>
                </div>
            </div>
        </div>

        <div class="section-card">
            <h3>Integration Requirements</h3>
            <div class="info-box">
                <p><strong>Ready for Integration:</strong></p>
                <ul>
                    <li>✅ User profile management - <strong>Ready</strong></li>
                    <li>✅ Enrollment and verification workflow - <strong>Ready</strong></li>
                    <li>✅ Fraud detection framework - <strong>Ready</strong></li>
                    <li>✅ Voice sample storage - <strong>Ready</strong></li>
                    <li>⚠️ Voice biometric engine - <strong>Required (Nuance/Pindrop/AWS)</strong></li>
                </ul>
                <p style="margin-top: 15px;"><strong>Supported Providers:</strong></p>
                <ul>
                    <li>Nuance Gatekeeper - Enterprise voice biometrics</li>
                    <li>Pindrop - Voice authentication and fraud detection</li>
                    <li>AWS Connect Voice ID - Scalable cloud solution</li>
                </ul>
            </div>
        </div>
    `}var f=async()=>{try{let e=(await(await fetch(`/api/framework/voice-biometrics/profiles`,{headers:pbxAuthHeaders()})).json()).profiles??[],t=document.getElementById(`biometric-profiles-list`);if(e.length===0){t.innerHTML=`<p style="color: #666;">No users enrolled yet. Framework ready for voice enrollment.</p>`;return}t.innerHTML=`
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Extension</th>
                        <th>User Name</th>
                        <th>Enrollment Date</th>
                        <th>Verifications</th>
                        <th>Last Verified</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${e.map(e=>`
                        <tr>
                            <td>${e.extension}</td>
                            <td>${e.name}</td>
                            <td>${new Date(e.enrolled_at).toLocaleDateString()}</td>
                            <td>${e.verification_count||0}</td>
                            <td>${e.last_verified?new Date(e.last_verified).toLocaleString():`Never`}</td>
                            <td><span class="status-badge status-${e.status}">${e.status}</span></td>
                            <td>
                                <button onclick="deleteVoiceProfile('${e.id}')" class="btn-danger btn-sm">Delete</button>
                            </td>
                        </tr>
                    `).join(``)}
                </tbody>
            </table>
        `}catch{document.getElementById(`biometric-profiles-list`).innerHTML=`<p style="color: #666;">Framework ready. Voice profiles will appear when feature is enabled.</p>`}},ue=async()=>{try{let e=(await(await fetch(`/api/framework/voice-biometrics/statistics`,{headers:pbxAuthHeaders()})).json()).statistics??{};document.getElementById(`enrolled-users`).textContent=e.enrolled_users||0,document.getElementById(`verifications-today`).textContent=e.verifications_today||0,document.getElementById(`success-rate`).textContent=`${e.success_rate??0}%`,document.getElementById(`fraud-attempts`).textContent=e.fraud_attempts||0}catch{}};function de(){let e=document.getElementById(`enroll-user-dialog`);e&&e.remove()}function fe(){de(),document.body.insertAdjacentHTML(`beforeend`,`
        <div id="enroll-user-dialog" class="modal" style="display: flex; align-items: center;
             justify-content: center;">
            <div class="modal-content" style="max-width: 500px;">
                <div class="modal-header">
                    <h3>Enroll User for Voice Biometrics</h3>
                    <span class="close" onclick="hideEnrollUserDialog()">&times;</span>
                </div>
                <form id="enroll-user-form">
                    <div class="form-group">
                        <label>User ID / Extension:</label>
                        <input type="text" name="user_id" required class="form-control"
                            placeholder="1001">
                        <small>Enter the user ID or extension number to enroll</small>
                    </div>
                    <div class="info-box" style="margin-top: 15px;">
                        <p><strong>Enrollment Process:</strong></p>
                        <ol style="margin: 5px 0 0 20px;">
                            <li>Start enrollment session</li>
                            <li>Record voice samples (3-5 phrases)</li>
                            <li>Biometric engine processes voiceprint</li>
                            <li>Profile activated for authentication</li>
                        </ol>
                    </div>
                    <div class="modal-actions">
                        <button type="submit" class="btn-primary">Start Enrollment</button>
                        <button type="button" class="btn-secondary"
                            onclick="hideEnrollUserDialog()">Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    `),document.getElementById(`enroll-user-form`).onsubmit=async function(e){e.preventDefault();let t={user_id:new FormData(e.target).get(`user_id`)};try{let e=await(await fetch(`/api/framework/voice-biometrics/enroll`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify(t)})).json();e.success?(alert(`Enrollment started for ${e.user_id}.\n\nSession ID: ${e.session_id}\nRequired samples: ${e.required_samples}\n\nPlease complete voice sample recording via the phone system.`),de(),f()):alert(`Error starting enrollment: ${e.error??`Unknown error`}`)}catch(e){alert(`Error starting enrollment: `+e.message)}}}var pe=async e=>{if(confirm(`Are you sure you want to delete this voice profile?`))try{await fetch(`/api/framework/voice-biometrics/profile/${e}`,{method:`DELETE`,headers:pbxAuthHeaders()}),await f()}catch(e){alert(`Error: ${e.message}`)}};function me(){return`
        <h2>📊 Call Quality Prediction</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>Proactive network issue detection using machine learning.</p>
            <p><strong>Features:</strong> Real-time quality prediction, network metrics tracking, proactive alerting</p>
        </div>

        <div class="section-card">
            <h3>Prediction Configuration</h3>
            <p>Configure quality prediction settings here. Framework ready for ML model integration.</p>
            <div class="info-box">
                <p><strong>Ready for Integration:</strong></p>
                <ul>
                    <li>✅ Network metrics collection (latency, jitter, packet loss)</li>
                    <li>✅ Alert threshold configuration</li>
                    <li>✅ Historical trend analysis framework</li>
                    <li>⚠️ Requires ML prediction model</li>
                </ul>
            </div>
        </div>
    `}function he(){return`
        <h2>🎬 Video Codecs (H.264/H.265)</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>Advanced video codec support for H.264 and H.265 video calling.</p>
            <p><strong>Supported Codecs:</strong> FFmpeg, OpenH264, x265</p>
            <p><strong>Features:</strong> Codec negotiation, bandwidth calculation, encoder/decoder creation</p>
        </div>

        <div class="section-card">
            <h3>Codec Configuration</h3>
            <p>Configure video codec settings here. Framework ready for video engine integration.</p>
            <div class="info-box">
                <p><strong>Ready for Integration:</strong></p>
                <ul>
                    <li>✅ Codec negotiation framework</li>
                    <li>✅ Bandwidth calculation</li>
                    <li>✅ Resolution and bitrate management</li>
                    <li>⚠️ Requires FFmpeg or codec library integration</li>
                </ul>
            </div>
        </div>
    `}function ge(){return(async()=>{try{let e=await(await fetch(`/api/framework/bi-integration/statistics`,{headers:pbxAuthHeaders()})).json();document.getElementById(`bi-stats-display`).innerHTML=`
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-icon">📦</div>
                        <div class="stat-value">${e.total_datasets||0}</div>
                        <div class="stat-label">Datasets</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📤</div>
                        <div class="stat-value">${e.total_exports||0}</div>
                        <div class="stat-label">Total Exports</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">⏰</div>
                        <div class="stat-value">${e.last_export_time?new Date(e.last_export_time).toLocaleDateString():`Never`}</div>
                        <div class="stat-label">Last Export</div>
                    </div>
                </div>
            `}catch(e){console.error(`Error loading BI statistics:`,e)}})(),`
        <h2>📈 Business Intelligence Integration</h2>
        <div class="info-box" style="background: #e8f5e9; border-left: 4px solid #4caf50;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-fully-implemented">✅ API Connected</span>
                <strong>REST API Endpoints Active</strong>
            </div>
            <p>Export call data to business intelligence tools.</p>
            <p><strong>Supported Tools:</strong> Tableau, Power BI, Looker, Qlik, Metabase</p>
            <p><strong>Export Formats:</strong> CSV, JSON, Excel</p>
        </div>

        <div class="section-card">
            <h3>Statistics</h3>
            <div id="bi-stats-display">
                <p>Loading statistics...</p>
            </div>
        </div>

        <div class="section-card">
            <h3>Available Datasets</h3>
            <div id="bi-datasets-list">
                <div class="stats-grid" style="grid-template-columns: repeat(auto-fit, minmax(250px, 1fr));">
                    <div class="stat-card">
                        <h4>📞 Call Detail Records (CDR)</h4>
                        <p style="color: #666; font-size: 14px; margin: 10px 0;">Complete call history with caller, callee, duration, and disposition</p>
                        <button onclick="exportBIDataset('cdr')" class="btn-primary btn-sm">Export CDR</button>
                    </div>
                    <div class="stat-card">
                        <h4>📊 Queue Statistics</h4>
                        <p style="color: #666; font-size: 14px; margin: 10px 0;">Call queue metrics, wait times, and agent performance</p>
                        <button onclick="exportBIDataset('queue_stats')" class="btn-primary btn-sm">Export Queue Stats</button>
                    </div>
                    <div class="stat-card">
                        <h4>📡 QoS Metrics</h4>
                        <p style="color: #666; font-size: 14px; margin: 10px 0;">Call quality data including MOS, jitter, packet loss</p>
                        <button onclick="exportBIDataset('qos_metrics')" class="btn-primary btn-sm">Export QoS</button>
                    </div>
                    <div class="stat-card">
                        <h4>👥 Extension Usage</h4>
                        <p style="color: #666; font-size: 14px; margin: 10px 0;">Per-extension usage, call volumes, and trends</p>
                        <button onclick="exportBIDataset('extension_usage')" class="btn-primary btn-sm">Export Analytics</button>
                    </div>
                </div>
            </div>
        </div>

        <div class="section-card">
            <h3>Export Configuration</h3>
            <form id="bi-export-form" style="max-width: 600px;">
                <div class="form-group">
                    <label>Export Format:</label>
                    <select id="bi-export-format" class="form-control">
                        <option value="csv">CSV (Comma-Separated Values)</option>
                        <option value="json">JSON (JavaScript Object Notation)</option>
                        <option value="excel">Excel (.xlsx)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Date Range:</label>
                    <select id="bi-date-range" class="form-control">
                        <option value="today">Today</option>
                        <option value="yesterday">Yesterday</option>
                        <option value="last7days" selected>Last 7 Days</option>
                        <option value="last30days">Last 30 Days</option>
                        <option value="last90days">Last 90 Days</option>
                    </select>
                </div>
            </form>
        </div>

        <div class="section-card">
            <h3>BI Tool Integration</h3>
            <div class="info-box">
                <p><strong>Integration Status:</strong></p>
                <ul>
                    <li>✅ Default datasets (CDR, queue stats, QoS metrics) - <strong>Ready</strong></li>
                    <li>✅ Multiple export formats (CSV, JSON, Excel) - <strong>Ready</strong></li>
                    <li>✅ REST API endpoints for data export - <strong>Active</strong></li>
                    <li>✅ Date range filtering - <strong>Ready</strong></li>
                    <li>⚠️ Direct BI tool API connections - <strong>Requires credentials</strong></li>
                </ul>
                <p style="margin-top: 15px;"><strong>Active API Endpoints:</strong></p>
                <pre style="background: #f5f5f5; padding: 10px; border-radius: 4px; overflow-x: auto;">
GET  /api/framework/bi-integration/datasets
GET  /api/framework/bi-integration/statistics
GET  /api/framework/bi-integration/export/{dataset}
POST /api/framework/bi-integration/export
POST /api/framework/bi-integration/dataset
POST /api/framework/bi-integration/test-connection</pre>
            </div>
        </div>
    `}var _e=async e=>{let t=document.getElementById(`bi-export-format`)?.value||`csv`,n=document.getElementById(`bi-date-range`)?.value||`last7days`,r=event.target,i=r.textContent;r.textContent=`Exporting...`,r.disabled=!0;try{let r=await(await fetch(`/api/framework/bi-integration/export`,{method:`POST`,headers:pbxAuthHeaders(),body:JSON.stringify({dataset:e,format:t,date_range:n})})).json();r.success?alert(`✅ Export successful!\n\nDataset: ${r.dataset}\nFormat: ${r.format}\nFile: ${r.file_path}\n\nThe export has been created on the server.`):alert(`❌ Export failed: ${r.error}`)}catch(e){alert(`❌ Export error: ${e.message}`)}finally{r.textContent=i,r.disabled=!1}};function ve(){return(async()=>{try{let e=await(await fetch(`/api/framework/call-tagging/statistics`,{headers:pbxAuthHeaders()})).json();document.getElementById(`tagging-stats-display`).innerHTML=`
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-icon">🏷️</div>
                        <div class="stat-value">${e.total_calls_tagged||0}</div>
                        <div class="stat-label">Calls Tagged</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📝</div>
                        <div class="stat-value">${e.custom_tags_count||0}</div>
                        <div class="stat-label">Custom Tags</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">⚙️</div>
                        <div class="stat-value">${e.tagging_rules_count||0}</div>
                        <div class="stat-label">Active Rules</div>
                    </div>
                </div>
            `}catch(e){console.error(`Error loading tagging statistics:`,e)}})(),setTimeout(()=>{p(),m()},100),`
        <h2>🏷️ Call Tagging & Categorization</h2>
        <div class="info-box" style="background: #e8f5e9; border-left: 4px solid #4caf50;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-fully-implemented">✅ API Connected</span>
                <strong>REST API Endpoints Active</strong>
            </div>
            <p>AI-powered call classification and auto-tagging.</p>
            <p><strong>Features:</strong> Auto-tagging, rule-based tagging, tag analytics, search by tags</p>
        </div>

        <div class="section-card">
            <h3>Statistics</h3>
            <div id="tagging-stats-display">
                <p>Loading statistics...</p>
            </div>
        </div>

        <div class="section-card">
            <h3>Tag Management</h3>
            <button onclick="showCreateTagDialog()" class="btn-primary">+ Create Tag</button>
            <div id="tags-list" style="margin-top: 15px;">
                <p>Loading tags...</p>
            </div>
        </div>

        <div class="section-card">
            <h3>Auto-Tagging Rules</h3>
            <button onclick="showCreateRuleDialog()" class="btn-primary">+ Create Rule</button>
            <div id="tagging-rules-list" style="margin-top: 15px;">
                <p>Loading rules...</p>
            </div>
        </div>

        <div class="section-card">
            <h3>API Integration</h3>
            <div class="info-box">
                <p><strong>Integration Status:</strong></p>
                <ul>
                    <li>✅ Tag creation and management - <strong>Active</strong></li>
                    <li>✅ Rule-based auto-tagging - <strong>Ready</strong></li>
                    <li>✅ Tag search and analytics - <strong>Ready</strong></li>
                    <li>✅ REST API endpoints - <strong>Active</strong></li>
                    <li>⚠️ AI classification service - <strong>Requires external AI</strong></li>
                </ul>
                <p style="margin-top: 15px;"><strong>Active API Endpoints:</strong></p>
                <pre style="background: #f5f5f5; padding: 10px; border-radius: 4px; overflow-x: auto;">
GET  /api/framework/call-tagging/tags
GET  /api/framework/call-tagging/rules
GET  /api/framework/call-tagging/statistics
POST /api/framework/call-tagging/tag
POST /api/framework/call-tagging/rule
POST /api/framework/call-tagging/classify/{call_id}</pre>
                <p style="margin-top: 15px;"><strong>Supported AI Services:</strong></p>
                <ul>
                    <li>OpenAI GPT for semantic classification</li>
                    <li>Google Cloud Natural Language</li>
                    <li>AWS Comprehend</li>
                    <li>Custom ML models via REST API</li>
                </ul>
            </div>
        </div>
    `}var p=async()=>{try{let e=(await(await fetch(`/api/framework/call-tagging/tags`,{headers:pbxAuthHeaders()})).json()).tags??[],t=document.getElementById(`tags-list`);if(e.length===0){t.innerHTML=`<p style="color: #666;">No tags created yet. Click "+ Create Tag" to add your first tag.</p>`;return}t.innerHTML=`
            <div style="display: flex; flex-wrap: wrap; gap: 10px;">
                ${e.map(e=>`
                    <div class="tag-badge" style="background: ${e.color||`#4caf50`}; color: white; padding: 8px 15px; border-radius: 20px; display: flex; align-items: center; gap: 8px;">
                        <span>${e.name}</span>
                        <span style="font-size: 11px; opacity: 0.8;">(${e.count||0} calls)</span>
                        <button onclick="deleteTag('${e.id}')" style="background: none; border: none; color: white; cursor: pointer; padding: 0 5px;">×</button>
                    </div>
                `).join(``)}
            </div>
        `}catch{document.getElementById(`tags-list`).innerHTML=`<p class="text-muted">Framework ready. Tags will appear when feature is enabled.</p>`}},m=async()=>{try{let e=(await(await fetch(`/api/framework/call-tagging/rules`,{headers:pbxAuthHeaders()})).json()).rules??[],t=document.getElementById(`tagging-rules-list`);if(e.length===0){t.innerHTML=`<p style="color: #666;">No auto-tagging rules created yet.</p>`;return}t.innerHTML=`
            <table class="data-table">
                <thead>
                    <tr>
                        <th>Rule Name</th>
                        <th>Condition</th>
                        <th>Tag</th>
                        <th>Status</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    ${e.map(e=>`
                        <tr>
                            <td>${e.name}</td>
                            <td>${e.condition}</td>
                            <td><span class="tag-badge" style="background: ${e.tag_color}; color: white; padding: 4px 10px; border-radius: 12px;">${e.tag_name}</span></td>
                            <td>${e.enabled?`✅ Active`:`❌ Disabled`}</td>
                            <td>
                                <button onclick="toggleRule('${e.id}')" class="btn-secondary btn-sm">${e.enabled?`Disable`:`Enable`}</button>
                                <button onclick="deleteRule('${e.id}')" class="btn-danger btn-sm">Delete</button>
                            </td>
                        </tr>
                    `).join(``)}
                </tbody>
            </table>
        `}catch{document.getElementById(`tagging-rules-list`).innerHTML=`<p class="text-muted">Framework ready. Rules will appear when feature is enabled.</p>`}},ye=async()=>{try{let e=(await(await fetch(`/api/framework/call-tagging/statistics`,{headers:pbxAuthHeaders()})).json()).statistics??{};document.getElementById(`total-tags`).textContent=e.total_tags||0,document.getElementById(`tagged-calls`).textContent=e.tagged_calls||0,document.getElementById(`active-rules`).textContent=e.active_rules||0,document.getElementById(`auto-tagged`).textContent=e.auto_tagged_today||0}catch{}};function be(){let e=document.getElementById(`create-tag-dialog`);e&&e.remove()}function xe(){be(),document.body.insertAdjacentHTML(`beforeend`,`
        <div id="create-tag-dialog" class="modal" style="display: flex; align-items: center;
             justify-content: center;">
            <div class="modal-content" style="max-width: 500px;">
                <div class="modal-header">
                    <h3>Create Tag</h3>
                    <span class="close" onclick="hideCreateTagDialog()">&times;</span>
                </div>
                <form id="create-tag-form">
                    <div class="form-group">
                        <label>Tag Name:</label>
                        <input type="text" name="name" required class="form-control"
                            placeholder="VIP Customer">
                    </div>
                    <div class="form-group">
                        <label>Description:</label>
                        <input type="text" name="description" class="form-control"
                            placeholder="Calls from VIP customers">
                    </div>
                    <div class="form-group">
                        <label>Color:</label>
                        <input type="color" name="color" class="form-control"
                            value="#007bff" style="height: 40px; padding: 2px;">
                    </div>
                    <div class="modal-actions">
                        <button type="submit" class="btn-primary">Create Tag</button>
                        <button type="button" class="btn-secondary"
                            onclick="hideCreateTagDialog()">Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    `),document.getElementById(`create-tag-form`).onsubmit=async function(e){e.preventDefault();let t=new FormData(e.target),n={name:t.get(`name`),description:t.get(`description`)||``,color:t.get(`color`)||`#007bff`};try{let e=await(await fetch(`/api/framework/call-tagging/tag`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify(n)})).json();e.success?(alert(`Tag created successfully!`),be(),p(),ye()):alert(`Error creating tag: ${e.error??`Unknown error`}`)}catch(e){alert(`Error creating tag: `+e.message)}}}function Se(){let e=document.getElementById(`create-rule-dialog`);e&&e.remove()}function Ce(){Se(),document.body.insertAdjacentHTML(`beforeend`,`
        <div id="create-rule-dialog" class="modal" style="display: flex; align-items: center;
             justify-content: center;">
            <div class="modal-content" style="max-width: 600px;">
                <div class="modal-header">
                    <h3>Create Tagging Rule</h3>
                    <span class="close" onclick="hideCreateRuleDialog()">&times;</span>
                </div>
                <form id="create-rule-form">
                    <div class="form-group">
                        <label>Rule Name:</label>
                        <input type="text" name="name" required class="form-control"
                            placeholder="Long calls">
                    </div>
                    <div class="form-group">
                        <label>Tag ID to Apply:</label>
                        <input type="text" name="tag_id" required class="form-control"
                            placeholder="vip-customer">
                        <small>The tag that will be applied when this rule matches</small>
                    </div>
                    <div class="form-group">
                        <label>Priority:</label>
                        <input type="number" name="priority" class="form-control"
                            value="100" min="1" max="1000">
                        <small>Lower number = higher priority</small>
                    </div>
                    <div class="form-group">
                        <label>Condition Type:</label>
                        <select name="condition_type" class="form-control">
                            <option value="duration_gt">Call Duration Greater Than</option>
                            <option value="duration_lt">Call Duration Less Than</option>
                            <option value="caller_pattern">Caller Pattern (Regex)</option>
                            <option value="callee_pattern">Callee Pattern (Regex)</option>
                            <option value="keyword">Keyword in Transcription</option>
                            <option value="time_range">Time of Day Range</option>
                        </select>
                    </div>
                    <div class="form-group">
                        <label>Condition Value:</label>
                        <input type="text" name="condition_value" required class="form-control"
                            placeholder="300 (seconds), ^\\+1555.*, escalate, 09:00-17:00">
                        <small>Value depends on condition type selected above</small>
                    </div>
                    <div class="modal-actions">
                        <button type="submit" class="btn-primary">Create Rule</button>
                        <button type="button" class="btn-secondary"
                            onclick="hideCreateRuleDialog()">Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    `),document.getElementById(`create-rule-form`).onsubmit=async function(e){e.preventDefault();let t=new FormData(e.target),n={name:t.get(`name`),tag_id:t.get(`tag_id`),priority:parseInt(t.get(`priority`))||100,conditions:[{type:t.get(`condition_type`),value:t.get(`condition_value`)}]};try{let e=await(await fetch(`/api/framework/call-tagging/rule`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify(n)})).json();e.success?(alert(`Rule created successfully!`),Se(),m(),ye()):alert(`Error creating rule: ${e.error??`Unknown error`}`)}catch(e){alert(`Error creating rule: `+e.message)}}}var we=async e=>{if(confirm(`Are you sure you want to delete this tag?`))try{await fetch(`/api/framework/call-tagging/tags/${e}`,{method:`DELETE`,headers:pbxAuthHeaders()}),await p()}catch(e){alert(`Error: ${e.message}`)}},Te=async e=>{if(confirm(`Are you sure you want to delete this rule?`))try{await fetch(`/api/framework/call-tagging/rules/${e}`,{method:`DELETE`,headers:pbxAuthHeaders()}),await m()}catch(e){alert(`Error: ${e.message}`)}},Ee=async e=>{try{await fetch(`/api/framework/call-tagging/rules/${e}/toggle`,{method:`POST`,headers:pbxAuthHeaders()}),await m()}catch(e){alert(`Error: ${e.message}`)}};function De(){return`
        <h2>📱 Mobile Apps Framework</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Backend Infrastructure Ready</strong>
            </div>
            <p>Full-featured mobile client support for iOS and Android.</p>
            <p><strong>Platforms:</strong> iOS (Swift/SwiftUI), Android (Kotlin)</p>
            <p><strong>Features:</strong> SIP calling, push notifications, device management, background call handling</p>
        </div>

        <div class="section-card">
            <h3>Configuration</h3>
            <form id="mobile-apps-config-form" style="max-width: 600px;" onsubmit="submitMobileAppsConfig(event)">
                <div class="form-group">
                    <label>
                        <input type="checkbox" id="mobile-apps-enabled" name="enabled">
                        Enable Mobile App Support
                    </label>
                </div>
                <div class="form-group">
                    <label>
                        <input type="checkbox" id="ios-enabled" name="ios_enabled" checked>
                        iOS Support
                    </label>
                </div>
                <div class="form-group">
                    <label>
                        <input type="checkbox" id="android-enabled" name="android_enabled" checked>
                        Android Support
                    </label>
                </div>
                <div class="form-group">
                    <label>
                        <input type="checkbox" id="push-enabled" name="push_enabled" checked>
                        Push Notifications
                    </label>
                </div>
                <div class="form-group">
                    <label>Firebase Server Key (for push notifications):</label>
                    <input type="password" id="firebase-key" name="firebase_key" class="form-control" placeholder="Your FCM server key">
                    <small>Required for iOS and Android push notifications</small>
                </div>
                <h4 style="margin-top: 20px;">Register Test Device</h4>
                <div class="form-group">
                    <label>User ID / Extension:</label>
                    <input type="text" id="mobile-user-id" name="user_id" class="form-control" placeholder="1001">
                </div>
                <div class="form-group">
                    <label>Device Token:</label>
                    <input type="text" id="mobile-device-token" name="device_token" class="form-control" placeholder="FCM or APNs device token">
                </div>
                <div class="form-group">
                    <label>Platform:</label>
                    <select id="mobile-platform" name="platform" class="form-control">
                        <option value="ios">iOS</option>
                        <option value="android">Android</option>
                        <option value="web">Web</option>
                    </select>
                </div>
                <div class="form-actions">
                    <button type="submit" class="btn-primary">Register Device</button>
                    <button type="button" class="btn-secondary" onclick="loadMobileAppsStats()">View Statistics</button>
                </div>
            </form>
        </div>

        <div class="section-card">
            <h3>Registered Devices</h3>
            <div id="mobile-devices-list">
                <p>No mobile devices registered yet.</p>
            </div>
        </div>

        <div class="section-card">
            <h3>Development Requirements</h3>
            <div class="info-box">
                <p><strong>To deploy mobile apps:</strong></p>
                <ul>
                    <li>✅ Device registration backend - <strong>Ready</strong></li>
                    <li>✅ Push notification framework - <strong>Ready</strong></li>
                    <li>✅ SIP configuration API - <strong>Ready</strong></li>
                    <li>⚠️ iOS app development (Swift/SwiftUI) - <strong>Required</strong></li>
                    <li>⚠️ Android app development (Kotlin) - <strong>Required</strong></li>
                    <li>⚠️ Firebase/APNs configuration - <strong>Required</strong></li>
                </ul>
                <p style="margin-top: 15px;"><strong>Recommended SIP libraries:</strong></p>
                <ul>
                    <li>iOS: PushKit + CallKit integration</li>
                    <li>Android: PJSIP or Linphone SDK</li>
                    <li>Both: WebRTC for media handling</li>
                </ul>
            </div>
        </div>
    `}var Oe=async()=>{let e=document.getElementById(`mobile-devices-list`);e.innerHTML=`<p>Loading device statistics...</p>`;try{let t=await(await fetch(`/api/mobile-push/devices`,{headers:pbxAuthHeaders()})).json(),n=t.devices??[],r=t.statistics??{},i=`
            <div class="stats-grid" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); margin-bottom: 20px;">
                <div class="stat-card">
                    <div class="stat-value">${r.total_devices||n.length||0}</div>
                    <div class="stat-label">Total Devices</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${r.ios_devices||0}</div>
                    <div class="stat-label">iOS Devices</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${r.android_devices||0}</div>
                    <div class="stat-label">Android Devices</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">${r.active_devices||0}</div>
                    <div class="stat-label">Active</div>
                </div>
            </div>
        `;n.length>0?i+=`
                <h4>Registered Devices</h4>
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Extension</th>
                            <th>Platform</th>
                            <th>Device Model</th>
                            <th>Push Token</th>
                            <th>Registered</th>
                            <th>Last Seen</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${n.map(e=>`
                            <tr>
                                <td>${e.extension}</td>
                                <td>${e.platform===`ios`?`📱 iOS`:`🤖 Android`}</td>
                                <td>${e.device_model||`Unknown`}</td>
                                <td><code style="font-size: 11px;">${(e.push_token||``).substring(0,20)}...</code></td>
                                <td>${new Date(e.registered_at).toLocaleString()}</td>
                                <td>${e.last_seen?new Date(e.last_seen).toLocaleString():`Never`}</td>
                            </tr>
                        `).join(``)}
                    </tbody>
                </table>
            `:i+=`<p><em>No devices registered yet. Devices will appear here once the mobile apps are deployed and users register.</em></p>`,e.innerHTML=i}catch{e.innerHTML=`
            <div class="stats-grid" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); margin-bottom: 20px;">
                <div class="stat-card">
                    <div class="stat-value">0</div>
                    <div class="stat-label">Total Devices</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">0</div>
                    <div class="stat-label">iOS Devices</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">0</div>
                    <div class="stat-label">Android Devices</div>
                </div>
                <div class="stat-card">
                    <div class="stat-value">0</div>
                    <div class="stat-label">Active</div>
                </div>
            </div>
            <p style="color: #666;"><em>Framework ready. Devices will appear when mobile apps are deployed and users register.</em></p>
        `}},ke=async e=>{e.preventDefault();let t=e.target,n=new FormData(t),r=n.get(`user_id`),i=n.get(`device_token`);if(!r||!i){alert(`User ID and Device Token are required to register a device.`);return}let a={user_id:r,device_token:i,platform:n.get(`platform`)||`unknown`};try{let e=await(await fetch(`/api/mobile-push/register`,{method:`POST`,headers:{"Content-Type":`application/json`},body:JSON.stringify(a)})).json();e.success?(alert(`Device registered successfully!`),Oe()):alert(`Error registering device: ${e.error??`Unknown error`}`)}catch(e){alert(`Error registering device: `+e.message)}};function Ae(){return`
        <h2>🔄 Mobile Number Portability</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>Use business phone numbers on mobile devices.</p>
            <p><strong>Features:</strong> DID mapping, simultaneous ring (desk + mobile), business hours routing</p>
        </div>

        <div class="section-card">
            <h3>Number Portability Configuration</h3>
            <p>Configure mobile number portability here. Framework ready for mobile integration.</p>
            <div class="info-box">
                <p><strong>Ready for Integration:</strong></p>
                <ul>
                    <li>✅ DID to mobile device mapping</li>
                    <li>✅ Simultaneous ring configuration</li>
                    <li>✅ Business hours routing rules</li>
                    <li>⚠️ Requires mobile SIP client integration</li>
                </ul>
            </div>
        </div>
    `}function je(){return`
        <h2>🎙️ Call Recording Analytics</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>AI analysis of recorded calls.</p>
            <p><strong>Features:</strong> Sentiment analysis, keyword detection, compliance checking, quality scoring, summarization</p>
        </div>

        <div class="section-card">
            <h3>Analytics Configuration</h3>
            <p>Configure recording analytics here. Framework ready for NLP service integration.</p>
            <div class="info-box">
                <p><strong>Ready for Integration:</strong></p>
                <ul>
                    <li>✅ Recording metadata tracking</li>
                    <li>✅ Analysis result storage</li>
                    <li>✅ Keyword and sentiment framework</li>
                    <li>⚠️ Requires NLP/speech analytics service</li>
                </ul>
            </div>
        </div>
    `}function Me(){return(async()=>{try{let e=await(await fetch(`/api/framework/call-blending/statistics`,{headers:pbxAuthHeaders()})).json();document.getElementById(`blending-stats-display`).innerHTML=`
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-icon">👥</div>
                        <div class="stat-value">${e.total_agents||0}</div>
                        <div class="stat-label">Total Agents</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">✅</div>
                        <div class="stat-value">${e.available_agents||0}</div>
                        <div class="stat-label">Available</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📞</div>
                        <div class="stat-value">${e.total_blended_calls||0}</div>
                        <div class="stat-label">Blended Calls</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">📊</div>
                        <div class="stat-value">${Math.round((e.actual_blend_ratio||0)*100)}%</div>
                        <div class="stat-label">Inbound Ratio</div>
                    </div>
                </div>
            `}catch(e){console.error(`Error loading blending statistics:`,e)}})(),(async()=>{try{let e=(await(await fetch(`/api/framework/call-blending/agents`,{headers:pbxAuthHeaders()})).json()).agents??[],t=document.getElementById(`blending-agents-list`);if(e.length===0){t.innerHTML=`<p style="color: #666;">No agents registered for call blending yet.</p>`;return}t.innerHTML=`
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Agent ID</th>
                            <th>Extension</th>
                            <th>Mode</th>
                            <th>Status</th>
                            <th>Inbound Calls</th>
                            <th>Outbound Calls</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${e.map(e=>`
                            <tr>
                                <td>${e.agent_id}</td>
                                <td>${e.extension}</td>
                                <td>
                                    <select onchange="changeAgentMode('${e.agent_id}', this.value)">
                                        <option value="blended" ${e.mode===`blended`?`selected`:``}>Blended</option>
                                        <option value="inbound_only" ${e.mode===`inbound_only`?`selected`:``}>Inbound Only</option>
                                        <option value="outbound_only" ${e.mode===`outbound_only`?`selected`:``}>Outbound Only</option>
                                        <option value="auto" ${e.mode===`auto`?`selected`:``}>Auto</option>
                                    </select>
                                </td>
                                <td>
                                    <span class="status-badge ${e.available?`status-online`:`status-offline`}">
                                        ${e.available?`✅ Available`:`🔴 Unavailable`}
                                    </span>
                                </td>
                                <td>${e.inbound_calls_handled||0}</td>
                                <td>${e.outbound_calls_handled||0}</td>
                                <td>
                                    <button onclick="viewAgentDetails('${e.agent_id}')" class="btn-sm btn-primary">View</button>
                                </td>
                            </tr>
                        `).join(``)}
                    </tbody>
                </table>
            `}catch(e){console.error(`Error loading agents:`,e),document.getElementById(`blending-agents-list`).innerHTML=`<p style="color: #666;">No agents available.</p>`}})(),`
        <h2>🔀 Call Blending</h2>
        <div class="info-box" style="background: #e8f5e9; border-left: 4px solid #4caf50;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-fully-implemented">✅ API Connected</span>
                <strong>REST API Endpoints Active</strong>
            </div>
            <p>Mix inbound and outbound calls for agent efficiency.</p>
            <p><strong>Features:</strong> Dynamic mode switching, priority distribution, inbound surge protection, workload balancing</p>
        </div>

        <div class="section-card">
            <h3>Statistics</h3>
            <div id="blending-stats-display">
                <p>Loading statistics...</p>
            </div>
        </div>

        <div class="section-card">
            <h3>Registered Agents</h3>
            <div id="blending-agents-list">
                <p>Loading agents...</p>
            </div>
        </div>

        <div class="section-card">
            <h3>API Integration</h3>
            <div class="info-box">
                <p><strong>Integration Status:</strong></p>
                <ul>
                    <li>✅ Agent mode management - <strong>Active</strong></li>
                    <li>✅ Priority-based distribution - <strong>Ready</strong></li>
                    <li>✅ Workload balancing framework - <strong>Ready</strong></li>
                    <li>✅ REST API endpoints - <strong>Active</strong></li>
                    <li>⚠️ Queue system integration - <strong>Requires configuration</strong></li>
                </ul>
                <p style="margin-top: 15px;"><strong>Active API Endpoints:</strong></p>
                <pre style="background: #f5f5f5; padding: 10px; border-radius: 4px; overflow-x: auto;">
GET  /api/framework/call-blending/agents
GET  /api/framework/call-blending/statistics
GET  /api/framework/call-blending/agent/{agent_id}
POST /api/framework/call-blending/agent
POST /api/framework/call-blending/agent/{agent_id}/mode</pre>
            </div>
        </div>
    `}function Ne(){return`
        <h2>📭 Predictive Voicemail Drop</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>Auto-leave pre-recorded messages when voicemail is detected.</p>
            <p><strong>Features:</strong> Answering machine detection (AMD), pre-recorded message library, FCC compliance</p>
        </div>

        <div class="section-card">
            <h3>Voicemail Drop Configuration</h3>
            <p>Configure voicemail drop here. Framework ready for AMD integration.</p>
            <div class="info-box">
                <p><strong>Ready for Integration:</strong></p>
                <ul>
                    <li>✅ Message library management</li>
                    <li>✅ FCC compliance tracking</li>
                    <li>✅ Drop success/failure reporting</li>
                    <li>⚠️ Requires answering machine detection algorithm</li>
                </ul>
            </div>
        </div>
    `}function Pe(){return(async()=>{try{let e=await(await fetch(`/api/framework/geo-redundancy/statistics`,{headers:pbxAuthHeaders()})).json();document.getElementById(`geo-stats-display`).innerHTML=`
                <div class="stats-grid">
                    <div class="stat-card">
                        <div class="stat-icon">🌍</div>
                        <div class="stat-value">${e.total_regions||0}</div>
                        <div class="stat-label">Regions</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">✅</div>
                        <div class="stat-value">${e.active_region||`None`}</div>
                        <div class="stat-label">Active Region</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">🔄</div>
                        <div class="stat-value">${e.total_failovers||0}</div>
                        <div class="stat-label">Total Failovers</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-icon">🤖</div>
                        <div class="stat-value">${e.auto_failover?`Enabled`:`Disabled`}</div>
                        <div class="stat-label">Auto Failover</div>
                    </div>
                </div>
            `}catch(e){console.error(`Error loading geo statistics:`,e)}})(),(async()=>{try{let e=(await(await fetch(`/api/framework/geo-redundancy/regions`,{headers:pbxAuthHeaders()})).json()).regions??[],t=document.getElementById(`geo-regions-list`);if(e.length===0){t.innerHTML=`<p style="color: #666;">No regions configured yet. Click "+ Add Region" to create your first region.</p>`;return}t.innerHTML=`
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Region ID</th>
                            <th>Name</th>
                            <th>Location</th>
                            <th>Status</th>
                            <th>Health Score</th>
                            <th>Trunks</th>
                            <th>Priority</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        ${e.map(e=>{let t=`#4caf50`;return e.status===`failed`?t=`#f44336`:e.status===`standby`&&(t=`#ff9800`),`
                                <tr ${e.is_active?`style="background: #e8f5e9;"`:``}>
                                    <td>
                                        ${e.region_id}
                                        ${e.is_active?`<span class="status-badge status-online">ACTIVE</span>`:``}
                                    </td>
                                    <td>${e.name}</td>
                                    <td>${e.location}</td>
                                    <td>
                                        <span class="status-badge" style="background: ${t};">
                                            ${e.status.toUpperCase()}
                                        </span>
                                    </td>
                                    <td>${Math.round((e.health_score||0)*100)}%</td>
                                    <td>${e.trunk_count||0}</td>
                                    <td>${e.priority}</td>
                                    <td>
                                        ${e.is_active?``:`<button onclick="triggerFailover('${e.region_id}')" class="btn-sm btn-primary">Activate</button>`}
                                        <button onclick="viewRegionDetails('${e.region_id}')" class="btn-sm">Details</button>
                                    </td>
                                </tr>
                            `}).join(``)}
                    </tbody>
                </table>
            `}catch(e){console.error(`Error loading regions:`,e),document.getElementById(`geo-regions-list`).innerHTML=`<p style="color: #666;">No regions available.</p>`}})(),`
        <h2>🌍 Geographic Redundancy</h2>
        <div class="info-box" style="background: #e8f5e9; border-left: 4px solid #4caf50;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-fully-implemented">✅ API Connected</span>
                <strong>REST API Endpoints Active</strong>
            </div>
            <p>Multi-region trunk registration with automatic failover for disaster recovery.</p>
            <p><strong>Features:</strong> Regional health monitoring, automatic failover, priority-based region selection, data replication</p>
        </div>

        <div class="section-card">
            <h3>Statistics</h3>
            <div id="geo-stats-display">
                <p>Loading statistics...</p>
            </div>
        </div>

        <div class="section-card">
            <h3>Geographic Regions</h3>
            <button onclick="showCreateRegionDialog()" class="btn-primary">+ Add Region</button>
            <div id="geo-regions-list" style="margin-top: 15px;">
                <p>Loading regions...</p>
            </div>
        </div>

        <div class="section-card">
            <h3>API Integration</h3>
            <div class="info-box">
                <p><strong>Integration Status:</strong></p>
                <ul>
                    <li>✅ Region management - <strong>Active</strong></li>
                    <li>✅ Health check framework - <strong>Ready</strong></li>
                    <li>✅ Failover priority configuration - <strong>Ready</strong></li>
                    <li>✅ REST API endpoints - <strong>Active</strong></li>
                    <li>⚠️ Multi-region infrastructure - <strong>Requires deployment</strong></li>
                </ul>
                <p style="margin-top: 15px;"><strong>Active API Endpoints:</strong></p>
                <pre style="background: #f5f5f5; padding: 10px; border-radius: 4px; overflow-x: auto;">
GET  /api/framework/geo-redundancy/regions
GET  /api/framework/geo-redundancy/statistics
GET  /api/framework/geo-redundancy/region/{region_id}
POST /api/framework/geo-redundancy/region
POST /api/framework/geo-redundancy/region/{region_id}/failover</pre>
            </div>
        </div>
    `}function Fe(){return`
        <h2>🌐 DNS SRV Failover</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>Automatic server failover using DNS SRV records.</p>
            <p><strong>Features:</strong> Priority-based server selection, weight-based load balancing, health monitoring, SRV record caching</p>
        </div>

        <div class="section-card">
            <h3>DNS SRV Configuration</h3>
            <p>Configure DNS SRV failover here. Framework ready for DNS integration.</p>
            <div class="info-box">
                <p><strong>Ready for Integration:</strong></p>
                <ul>
                    <li>✅ SRV record parsing and caching</li>
                    <li>✅ Priority and weight handling</li>
                    <li>✅ Health monitoring framework</li>
                    <li>⚠️ Requires DNS SRV record configuration</li>
                </ul>
            </div>
        </div>
    `}function Ie(){return`
        <h2>🛡️ Session Border Controller</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>Enhanced security and NAT traversal for SIP communications.</p>
            <p><strong>Features:</strong> Topology hiding, protocol normalization, DoS protection, media relay, call admission control</p>
        </div>

        <div class="section-card">
            <h3>SBC Configuration</h3>
            <p>Configure session border controller here. Framework ready for SBC deployment.</p>
            <div class="info-box">
                <p><strong>Ready for Integration:</strong></p>
                <ul>
                    <li>✅ Security policy framework</li>
                    <li>✅ NAT traversal configuration</li>
                    <li>✅ Media relay settings</li>
                    <li>⚠️ Requires SBC appliance or software</li>
                </ul>
            </div>
        </div>
    `}function Le(){return`
        <h2>🗺️ Data Residency Controls</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>Geographic data storage options for regulatory compliance.</p>
            <p><strong>Features:</strong> Region-specific storage, cross-border transfer controls, GDPR compliance, compliance reporting</p>
        </div>

        <div class="section-card">
            <h3>Data Residency Configuration</h3>
            <p>Configure data residency controls here. Framework ready for multi-region storage.</p>
            <div class="info-box">
                <p><strong>Ready for Integration:</strong></p>
                <ul>
                    <li>✅ Region management</li>
                    <li>✅ Data classification framework</li>
                    <li>✅ Compliance reporting</li>
                    <li>⚠️ Requires multi-region storage infrastructure</li>
                </ul>
            </div>
        </div>
    `}function Re(){return`
        <h2>💬 Team Messaging</h2>
        <div class="info-box" style="background: #fff3cd; border-left: 4px solid #ff9800;">
            <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 10px;">
                <span class="status-badge status-framework-only">⚙️ Framework Only</span>
                <strong>Database & APIs Ready</strong>
            </div>
            <p>Internal team messaging and collaboration platform (FREE alternatives to Slack/Teams).</p>
            <p><strong>Supported Services:</strong> Matrix/Element, Rocket.Chat, Mattermost</p>
            <p><strong>Features:</strong> Channels, direct messages, file sharing, integrations, search</p>
        </div>

        <div class="section-card">
            <h3>Channel Management</h3>
            <p>Configure team messaging channels here. Framework ready for messaging server integration.</p>
            <div class="info-box">
                <p><strong>Ready for Integration:</strong></p>
                <ul>
                    <li>✅ Channel creation and management - <strong>Ready</strong></li>
                    <li>✅ Member management - <strong>Ready</strong></li>
                    <li>✅ Message storage framework - <strong>Ready</strong></li>
                    <li>✅ File attachment support - <strong>Ready</strong></li>
                    <li>⚠️ Messaging server (Matrix/Rocket.Chat) - <strong>Required</strong></li>
                </ul>
                <p style="margin-top: 15px;"><strong>Recommended Setup:</strong></p>
                <ul>
                    <li><strong>Matrix + Element:</strong> Federated, secure, feature-rich</li>
                    <li><strong>Rocket.Chat:</strong> Easy setup, familiar Slack-like interface</li>
                    <li><strong>Mattermost:</strong> Enterprise features, compliance focus</li>
                </ul>
            </div>
        </div>

        <div class="section-card">
            <h3>Integration Requirements</h3>
            <div class="info-box">
                <p><strong>To activate team messaging:</strong></p>
                <ol>
                    <li>Install Matrix Synapse, Rocket.Chat, or Mattermost server</li>
                    <li>Configure server connection in config.yml</li>
                    <li>Set up authentication integration</li>
                    <li>Create initial channels and invite team members</li>
                </ol>
                <p style="margin-top: 15px;"><strong>Example config.yml:</strong></p>
                <pre style="background: #f5f5f5; padding: 10px; border-radius: 4px; overflow-x: auto;">
features:
  team_messaging:
    enabled: true
    provider: 'matrix'  # or 'rocketchat', 'mattermost'
    server_url: 'https://matrix.example.com'
    api_token: 'your-api-token'</pre>
            </div>
        </div>

        <div class="section-card">
            <h3>💚 100% Free Options</h3>
            <div class="info-box" style="background: #e8f5e9;">
                <p><strong>All team messaging options are free and open source:</strong></p>
                <ul>
                    <li>✅ <strong>Matrix:</strong> FREE federated messaging (like email for chat)</li>
                    <li>✅ <strong>Rocket.Chat:</strong> FREE community edition with unlimited users</li>
                    <li>✅ <strong>Mattermost:</strong> FREE team edition</li>
                </ul>
                <p style="margin-top: 15px; font-weight: bold; color: #2e7d32;">💰 Total Cost: $0 vs $96-240/user/year for Slack/Teams!</p>
            </div>
        </div>
    `}window.frameworkFeatures={loadFrameworkOverview:a,loadClickToDialTab:o,loadVideoConferencingTab:l,loadConversationalAITab:te,loadPredictiveDialingTab:ie,loadVoiceBiometricsTab:le,loadCallQualityPredictionTab:me,loadVideoCodecTab:he,loadBIIntegrationTab:ge,loadCallTaggingTab:ve,loadMobileAppsTab:De,loadMobileNumberPortabilityTab:Ae,loadRecordingAnalyticsTab:je,loadCallBlendingTab:Me,loadVoicemailDropTab:Ne,loadGeographicRedundancyTab:Pe,loadDNSSRVFailoverTab:Fe,loadSessionBorderControllerTab:Ie,loadDataResidencyTab:Le,loadTeamMessagingTab:Re,viewClickToDialHistory:c,loadConversationalAIStats:ne,loadMobileAppsStats:Oe,exportBIDataset:_e,loadCallTags:p,loadTaggingRules:m,loadTagStatistics:ye,showCreateTagDialog:xe,showCreateRuleDialog:Ce,deleteTag:we,deleteRule:Te,toggleRule:Ee,loadPredictiveDialingCampaigns:d,loadPredictiveDialingStats:ae,showCreateCampaignDialog:se,toggleCampaign:ce,loadVoiceBiometricProfiles:f,loadVoiceBiometricStats:ue,showEnrollUserDialog:fe,hideEnrollUserDialog:de,deleteVoiceProfile:pe,hideCreateCampaignDialog:oe,hideCreateTagDialog:be,hideCreateRuleDialog:Se,submitConversationalAIConfig:re,submitMobileAppsConfig:ke};for(let e of Object.keys(window.frameworkFeatures))typeof window.frameworkFeatures[e]==`function`&&(window[e]=window.frameworkFeatures[e]);var ze=`80`,Be=`443`,Ve=`9000`,He=3e4;function h(){let e=document.querySelector(`meta[name="api-base-url"]`);return e&&e.content?e.content:window.location.port===Ve||window.location.port===``||window.location.port===ze||window.location.port===Be?window.location.origin:`${window.location.protocol}//${window.location.hostname||`localhost`}:${Ve}`}function g(){let e=localStorage.getItem(`pbx_token`),t={"Content-Type":`application/json`};return e&&(t.Authorization=`Bearer ${e}`),t}async function _(e,t={},n=He){if(t.signal)throw Error(`fetchWithTimeout does not support custom abort signals. Use the timeout parameter instead.`);let r=new AbortController,i=setTimeout(()=>r.abort(),n);try{return await fetch(e,{...t,signal:r.signal})}catch(e){throw e instanceof Error&&e.name===`AbortError`?Error(`Request timed out`):e}finally{clearTimeout(i)}}var v=new class{_state;_listeners;constructor(e){this._state={...e},this._listeners=new Map}get(e){return this._state[e]}set(e,t){this._state[e]=t;let n=this._listeners.get(e)??[];for(let e of n)e(t)}subscribe(e,t){return this._listeners.has(e)||this._listeners.set(e,[]),this._listeners.get(e).push(t),()=>{let n=this._listeners.get(e);if(!n)return;let r=n.indexOf(t);r>-1&&n.splice(r,1)}}getState(){return{...this._state}}}({currentUser:null,currentExtensions:[],currentTab:`dashboard`,isAuthenticated:!1,autoRefreshInterval:null});function y(e){return String(e).replace(/&/g,`&amp;`).replace(/</g,`&lt;`).replace(/>/g,`&gt;`).replace(/"/g,`&quot;`).replace(/'/g,`&#39;`)}async function Ue(e){try{await navigator.clipboard.writeText(e),b(`License data copied to clipboard!`,`success`)}catch(e){console.error(`Error copying to clipboard:`,e),b(`Failed to copy to clipboard`,`error`)}}function We(e){if(!e)return``;try{return new Date(e).toLocaleString()}catch{return e}}function Ge(e,t){return e.length<=t?e:e.substring(0,t)+`...`}function Ke(e){let t=new Date().getTime()-new Date(e).getTime(),n=Math.floor(t/(1e3*60*60)),r=Math.floor(t%(1e3*60*60)/(1e3*60));return n>0?`${n}h ${r}m`:`${r}m`}function qe(e){return{registered:`<span class="badge" style="background: #10b981;">&#x2705; Registered</span>`,unregistered:`<span class="badge" style="background: #6b7280;">&#x26AA; Unregistered</span>`,failed:`<span class="badge" style="background: #ef4444;">&#x274C; Failed</span>`,disabled:`<span class="badge" style="background: #9ca3af;">&#x23F8;&#xFE0F; Disabled</span>`,degraded:`<span class="badge" style="background: #f59e0b;">&#x26A0;&#xFE0F; Degraded</span>`}[e]||e}function Je(e){return{healthy:`<span class="badge" style="background: #10b981;">&#x1F49A; Healthy</span>`,warning:`<span class="badge" style="background: #f59e0b;">&#x26A0;&#xFE0F; Warning</span>`,critical:`<span class="badge" style="background: #f59e0b;">&#x1F534; Critical</span>`,down:`<span class="badge" style="background: #ef4444;">&#x1F480; Down</span>`}[e]||e}function Ye(e){return{1:`<span class="badge" style="background: #ef4444;">1 - Highest</span>`,2:`<span class="badge" style="background: #f97316;">2 - High</span>`,3:`<span class="badge" style="background: #eab308;">3 - Medium</span>`,4:`<span class="badge" style="background: #3b82f6;">4 - Low</span>`,5:`<span class="badge" style="background: #6b7280;">5 - Lowest</span>`}[e]||`<span class="badge">${e}</span>`}function Xe(e){return e>=4.3?`quality-excellent`:e>=4?`quality-good`:e>=3.6?`quality-fair`:e>=3.1?`quality-poor`:`quality-bad`}function Ze(e){let t=[];if(e.days_of_week){let n=[`Mon`,`Tue`,`Wed`,`Thu`,`Fri`,`Sat`,`Sun`],r=e.days_of_week.map(e=>n[e]).join(`, `);t.push(r)}return e.start_time&&e.end_time&&t.push(`${e.start_time}-${e.end_time}`),e.holidays===!0?t.push(`Holidays`):e.holidays===!1&&t.push(`Non-holidays`),t.length>0?t.join(` | `):`Always`}function Qe(e){let t=JSON.stringify(e,null,2),n=new Blob([t],{type:`application/json`}),r=URL.createObjectURL(n),i=document.createElement(`a`);i.href=r,i.download=`license_${e.issued_to.replace(/[^a-zA-Z0-9]/g,`_`).toLowerCase()}_${new Date().toISOString().split(`T`)[0]}.json`,document.body.appendChild(i),i.click(),document.body.removeChild(i),URL.revokeObjectURL(r)}window.formatDate=We,window.truncate=Ge,window.getDuration=Ke,window.getStatusBadge=qe,window.getHealthBadge=Je,window.getPriorityBadge=Ye,window.getQualityClass=Xe,window.getScheduleDescription=Ze,window.downloadLicense=Qe;var $e={enabled:!0,displayTime:8e3,maxErrors:5,showStackTrace:!0},et=[],tt=!1;function nt(e){tt=e}function b(e,t=`info`){if(tt&&t===`error`){debugLog(`[${t.toUpperCase()}] ${e}`);return}let n=document.createElement(`div`);n.className=`notification notification-${t}`,n.style.cssText=`
        position: fixed;
        top: 80px;
        right: 20px;
        max-width: 400px;
        padding: 15px 20px;
        background: ${t===`success`?`#10b981`:t===`error`?`#ef4444`:t===`warning`?`#f59e0b`:`#3b82f6`};
        color: white;
        border-radius: 8px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.15);
        z-index: 9999;
        animation: slideInRight 0.3s ease-out;
        font-size: 14px;
        line-height: 1.4;
    `,n.innerHTML=`<strong>${t===`success`?`✓`:t===`error`?`✗`:t===`warning`?`⚠`:`ℹ`}</strong> ${y(e)}`,document.body.appendChild(n),setTimeout(()=>{n.style.animation=`slideOutRight 0.3s ease-in`,setTimeout(()=>n.remove(),300)},5e3)}function x(e,t=``){if(!$e.enabled)return;let n=`error-${Date.now()}`,r=e.message||e.toString(),i=e.stack||``;et.push({id:n,message:r,context:t,stack:i,timestamp:new Date}),et.length>$e.maxErrors&&et.shift();let a=document.createElement(`div`);a.id=n,a.className=`error-notification`,a.style.cssText=`
        position: fixed;
        top: 70px;
        right: 20px;
        max-width: 450px;
        background: #f44336;
        color: white;
        padding: 15px 20px;
        border-radius: 8px;
        box-shadow: 0 4px 12px rgba(0,0,0,0.3);
        z-index: 10000;
        animation: slideIn 0.3s ease-out;
        font-family: monospace;
        font-size: 13px;
        line-height: 1.4;
    `;let o=`
        <div style="display: flex; justify-content: space-between; align-items: start; margin-bottom: 10px;">
            <strong style="font-size: 16px;">JavaScript Error</strong>
            <button id="close-${n}"
                    style="background: none; border: none; color: white; font-size: 20px; cursor: pointer; padding: 0; margin-left: 10px;">
                \u00D7
            </button>
        </div>
    `;t&&(o+=`<div style="margin-bottom: 5px;"><strong>Context:</strong> ${y(t)}</div>`),o+=`<div style="margin-bottom: 5px;"><strong>Message:</strong> ${y(r)}</div>`,$e.showStackTrace&&i&&(o+=`
            <details style="margin-top: 10px; cursor: pointer;">
                <summary style="font-weight: bold; margin-bottom: 5px;">Stack Trace (click to expand)</summary>
                <pre style="background: rgba(0,0,0,0.2); padding: 8px; border-radius: 4px; overflow-x: auto; font-size: 11px; margin: 5px 0 0 0;">${y(i)}</pre>
            </details>
        `),o+=`
        <div style="margin-top: 10px; font-size: 11px; opacity: 0.9;">
            Tip: Press F12 to open browser console for more details
        </div>
    `,a.innerHTML=o;let s=a.querySelector(`#close-${n}`);s&&s.addEventListener(`click`,()=>a.remove()),document.body.appendChild(a),setTimeout(()=>{document.getElementById(n)&&(a.style.animation=`slideOut 0.3s ease-in`,setTimeout(()=>a.remove(),300))},$e.displayTime),console.error(`[${t||`Error`}]`,r),i&&console.error(`Stack trace:`,i)}var rt=1e4,it=null;function at(){typeof window.loadEmergencyContacts==`function`&&window.loadEmergencyContacts(),typeof window.loadEmergencyHistory==`function`&&window.loadEmergencyHistory()}function ot(){typeof window.loadFraudAlerts==`function`&&window.loadFraudAlerts()}function st(){typeof window.loadCallbackQueue==`function`&&window.loadCallbackQueue()}function ct(e){it&&=(clearInterval(it),null);let t={dashboard:()=>window.loadDashboard?.(),analytics:()=>window.loadAnalytics?.(),calls:()=>window.loadCalls?.(),qos:()=>window.loadQoSMetrics?.(),emergency:at,"callback-queue":st,extensions:()=>window.loadExtensions?.(),phones:()=>window.loadRegisteredPhones?.(),atas:()=>window.loadRegisteredATAs?.(),"hot-desking":()=>window.loadHotDeskSessions?.(),voicemail:()=>window.loadVoicemailTab?.(),"fraud-detection":ot,"sbc-management":()=>window.loadSBCData?.()};t[e]&&(it=setInterval(()=>{try{let n=t[e];typeof n==`function`?n():console.error(`Auto-refresh function for ${e} is not a function:`,n)}catch(t){console.error(`Error during auto-refresh of ${e}:`,t),t instanceof Error&&t.message?.includes(`401`)&&debugWarn(`Authentication error during auto-refresh - user may need to re-login`)}},rt)),v.set(`autoRefreshInterval`,it)}function S(e){for(let e of document.querySelectorAll(`.tab-content`))e.classList.remove(`active`);for(let e of document.querySelectorAll(`.tab-button`))e.classList.remove(`active`);let t=document.getElementById(e);if(!t)console.error(`CRITICAL: Tab element with id '${e}' not found in DOM`),console.error(`This may indicate a UI template issue or incorrect tab name`),console.error(`Current tab name: "${e}"`);else{t.classList.add(`active`);let n=document.querySelector(`[data-tab="${e}"]`);n?n.classList.add(`active`):debugWarn(`Tab button for '${e}' not found`)}v.set(`currentTab`,e),ct(e);let n={dashboard:window.loadDashboard,analytics:window.loadAnalytics,extensions:window.loadExtensions,phones:window.loadRegisteredPhones,atas:window.loadRegisteredATAs,provisioning:window.loadProvisioning,"auto-attendant":window.loadAutoAttendantConfig,voicemail:window.loadVoicemailTab,paging:window.loadPagingData,"call-queues":window.loadQueuesData,calls:window.loadCalls,config:window.loadConfig,"features-status":window.loadFeaturesStatus,"webrtc-phone":window.loadWebRTCPhoneConfig,"license-management":window.initLicenseManagement,qos:window.loadQoSMetrics,"find-me-follow-me":window.loadFMFMExtensions,"time-routing":window.loadTimeRoutingRules,webhooks:window.loadWebhooks,"hot-desking":window.loadHotDeskSessions,"recording-retention":window.loadRetentionPolicies,"jitsi-integration":window.loadJitsiConfig,"matrix-integration":window.loadMatrixConfig,"espocrm-integration":window.loadEspoCRMConfig,"click-to-dial":window.loadClickToDialTab,"fraud-detection":window.loadFraudDetectionData,"nomadic-e911":window.loadNomadicE911Data,"callback-queue":window.loadCallbackQueue,"mobile-push":window.loadMobilePushConfig,"recording-announcements":window.loadRecordingAnnouncements,"speech-analytics":window.loadSpeechAnalyticsConfigs,compliance:window.loadComplianceData,"crm-integrations":window.loadCRMActivityLog,"opensource-integrations":window.loadOpenSourceIntegrations,"sbc-management":window.loadSBCData},r={emergency:[window.loadEmergencyContacts,window.loadEmergencyHistory],codecs:[window.loadCodecStatus,window.loadDTMFConfig],"sip-trunks":[window.loadSIPTrunks,window.loadTrunkHealth,window.loadInboundRoutes],"least-cost-routing":[window.loadLCRRates,window.loadLCRStatistics]},i=n[e];if(i)i();else{let t=r[e];if(t)for(let e of t)e?.()}}function lt(){let e=document.querySelectorAll(`.tab-button`);for(let t of e)t.addEventListener(`click`,()=>{let e=t.getAttribute(`data-tab`);e&&S(e)});let t=document.querySelector(`.sidebar`);t&&t.addEventListener(`keydown`,e=>{let t=e,n=t.target;if(!n.classList.contains(`tab-button`))return;let r=n.closest(`.sidebar-section`);if(!r)return;let i=Array.from(r.querySelectorAll(`.tab-button`)),a=i.indexOf(n),o=-1;t.key===`ArrowDown`?o=a<i.length-1?a+1:0:t.key===`ArrowUp`?o=a>0?a-1:i.length-1:t.key===`Home`?o=0:t.key===`End`&&(o=i.length-1),o>=0&&(t.preventDefault(),i[o]?.focus())}),document.addEventListener(`keydown`,e=>{if(e.key===`Escape`){let e=document.querySelector(`.modal.active`);e&&e.classList.remove(`active`)}})}async function ut(e,t=5,n=1e3){if(!Array.isArray(e))throw TypeError(`promiseFunctions must be an array`);let r=[];for(let i=0;i<e.length;i+=t){let a=e.slice(i,i+t).map(e=>typeof e==`function`?e():e),o=await Promise.allSettled(a);r.push(...o),i+t<e.length&&await new Promise(e=>setTimeout(e,n))}return r}async function dt(){let e=document.getElementById(`refresh-all-button`);if(!e||e.disabled)return;let t=e.textContent,n=e.disabled;try{e.textContent=`⏳ Refreshing All Tabs...`,e.disabled=!0,window.suppressErrorNotifications=!0,debugLog(`Refreshing all data for ALL tabs...`);let t=[];window.loadDashboard&&t.push(()=>window.loadDashboard()),window.loadADStatus&&t.push(()=>window.loadADStatus()),window.loadAnalytics&&t.push(()=>window.loadAnalytics()),window.loadExtensions&&t.push(()=>window.loadExtensions());let n=(await ut(t,5,1e3)).filter(e=>e.status===`rejected`);n.length>0&&debugLog(`${n.length} refresh operation(s) failed (expected for unavailable features):`,n.map(e=>e.reason?.message??e.reason)),b(`✅ All tabs refreshed successfully`,`success`)}catch(e){let t=e instanceof Error?e.message:String(e);console.error(`Error refreshing data:`,e),b(`Failed to refresh: ${t}`,`error`)}finally{window.suppressErrorNotifications=!1,e.textContent=t,e.disabled=n}}function ft(){let e=document.getElementById(`refresh-all-button`);e&&e.addEventListener(`click`,dt)}window.executeBatched=ut,window.refreshAllData=dt;var pt=6e4;async function mt(){try{let e=await _(`${h()}/api/status`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}: ${e.statusText}`);let t=await e.json();document.getElementById(`stat-extensions`).textContent=String(t.registered_extensions??0),document.getElementById(`stat-calls`).textContent=String(t.active_calls??0),document.getElementById(`stat-total-calls`).textContent=String(t.total_calls??0),document.getElementById(`stat-recordings`).textContent=String(t.active_recordings??0);let n=document.getElementById(`system-status`);n&&(n.textContent=`System: ${t.running?`Running`:`Stopped`}`,n.classList.remove(`connected`,`disconnected`),n.classList.add(`status-badge`,t.running?`connected`:`disconnected`)),gt()}catch(e){console.error(`Error loading dashboard:`,e);for(let e of[`stat-extensions`,`stat-calls`,`stat-total-calls`,`stat-recordings`]){let t=document.getElementById(e);t&&(t.textContent=`Error`)}b(`Failed to load dashboard: ${e instanceof Error?e.message:String(e)}`,`error`)}}function ht(){mt(),b(`Dashboard refreshed`,`success`)}async function gt(){try{let e=await _(`${h()}/api/integrations/ad/status`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}`);let t=await e.json(),n=document.getElementById(`ad-status-badge`);n&&(n.textContent=t.enabled?`Enabled`:`Disabled`,n.className=`status-badge ${t.enabled?`enabled`:`disabled`}`);let r=document.getElementById(`ad-connection-status`);r&&(r.textContent=t.connected?`✓ Connected`:`✗ Not Connected`,r.style.color=t.connected?`#10b981`:`#ef4444`);let i=e=>document.getElementById(e);i(`ad-server`)&&(i(`ad-server`).textContent=t.server??`Not configured`),i(`ad-auto-provision`)&&(i(`ad-auto-provision`).textContent=t.auto_provision?`Yes`:`No`),i(`ad-synced-users`)&&(i(`ad-synced-users`).textContent=String(t.synced_users??0));let a=i(`ad-error`);a&&(a.textContent=t.error??`None`,a.style.color=t.error?`#d32f2f`:`#10b981`);let o=i(`ad-sync-btn`);o&&(o.disabled=!(t.enabled&&t.connected))}catch(e){console.error(`Error loading AD status:`,e)}}function _t(){gt(),b(`AD status refreshed`,`success`)}async function vt(){let e=document.getElementById(`ad-sync-btn`);if(!e)return;let t=e.textContent;e.disabled=!0,e.textContent=`Syncing...`;try{let e=await _(`${h()}/api/integrations/ad/sync`,{method:`POST`,headers:g()},pt);if(!e.ok){let t=await e.json().catch(()=>({error:`HTTP ${e.status}`}));throw Error(t.error||`HTTP ${e.status}`)}let t=await e.json();t.success?(b(t.message||`Successfully synced ${t.synced_count} users`,`success`),gt()):b(t.error||`Failed to sync users`,`error`)}catch(e){console.error(`Error syncing AD users:`,e),b((e instanceof Error?e.message:String(e))===`Request timed out`?`AD sync timed out. Check server logs.`:`Error syncing AD users`,`error`)}finally{e.textContent=t,e.disabled=!1}}window.loadDashboard=mt,window.refreshDashboard=ht,window.loadADStatus=gt,window.refreshADStatus=_t,window.syncADUsers=vt;var yt=1e4;async function bt(){let e=document.getElementById(`extensions-table-body`);if(e){e.innerHTML=`<tr><td colspan="8" class="loading">Loading extensions...</td></tr>`;try{let t=await _(`${h()}/api/extensions`,{headers:g()},yt);if(!t.ok)throw Error(`HTTP error! status: ${t.status}`);let n=await t.json();if(window.currentExtensions=n,n.length===0){e.innerHTML=`<tr><td colspan="8" class="loading">No extensions found.</td></tr>`;return}let r=e=>{let t=``;return e.ad_synced&&(t+=` <span class="ad-badge" title="Synced from Active Directory">AD</span>`),e.is_admin&&(t+=` <span class="admin-badge" title="Admin Privileges">Admin</span>`),t};e.innerHTML=n.map(e=>`
            <tr>
                <td><strong>${y(e.number)}</strong>${r(e)}</td>
                <td>${y(e.name)}</td>
                <td>${e.email?y(e.email):`Not set`}</td>
                <td>${e.did_number?y(e.did_number):`Not set`}</td>
                <td class="${e.registered?`status-online`:`status-offline`}">
                    ${e.registered?`Online`:`Offline`}
                </td>
                <td>${e.allow_external?`Yes`:`No`}</td>
                <td>${e.voicemail_enabled?`Set`:`Not Set`}</td>
                <td>
                    <button class="btn btn-primary" onclick="editExtension('${y(e.number)}')">Edit</button>
                    ${e.registered?`<button class="btn btn-secondary" onclick="rebootPhone('${y(e.number)}')">Reboot</button>`:``}
                    <button class="btn btn-danger" onclick="deleteExtension('${y(e.number)}')">Delete</button>
                </td>
            </tr>
        `).join(``)}catch(t){console.error(`Error loading extensions:`,t),e.innerHTML=`<tr><td colspan="8" class="loading">${(t instanceof Error?t.message:String(t))===`Request timed out`?`Request timed out. System may still be starting.`:`Error loading extensions`}</td></tr>`}}}function xt(){let e=document.getElementById(`add-extension-modal`);e&&e.classList.add(`active`);let t=document.getElementById(`add-extension-form`);t&&t.reset()}function St(){let e=document.getElementById(`add-extension-modal`);e&&e.classList.remove(`active`)}function Ct(e){let t=(window.currentExtensions??[]).find(t=>t.number===e);if(!t)return;let n=e=>document.getElementById(e);n(`edit-ext-number`)&&(n(`edit-ext-number`).value=t.number),n(`edit-ext-name`)&&(n(`edit-ext-name`).value=t.name),n(`edit-ext-email`)&&(n(`edit-ext-email`).value=t.email??``),n(`edit-ext-did-number`)&&(n(`edit-ext-did-number`).value=t.did_number??``),n(`edit-ext-allow-external`)&&(n(`edit-ext-allow-external`).checked=!!t.allow_external),n(`edit-ext-is-admin`)&&(n(`edit-ext-is-admin`).checked=!!t.is_admin),n(`edit-ext-password`)&&(n(`edit-ext-password`).value=``);let r=document.getElementById(`edit-extension-modal`);r&&r.classList.add(`active`)}function wt(){let e=document.getElementById(`edit-extension-modal`);e&&e.classList.remove(`active`)}async function Tt(e){if(confirm(`Are you sure you want to delete extension ${e}?`))try{let t=h(),n=await fetch(`${t}/api/extensions/${e}`,{method:`DELETE`,headers:g()});n.ok?(b(`Extension deleted successfully`,`success`),bt()):b((await n.json()).error||`Failed to delete extension`,`error`)}catch(e){console.error(`Error deleting extension:`,e),b(`Failed to delete extension`,`error`)}}async function Et(e){if(confirm(`Reboot phone for extension ${e}?`))try{let t=h();(await fetch(`${t}/api/phones/${e}/reboot`,{method:`POST`,headers:g()})).ok?b(`Reboot command sent to ${e}`,`success`):b(`Failed to reboot phone`,`error`)}catch(e){console.error(`Error rebooting phone:`,e),b(`Failed to reboot phone`,`error`)}}async function Dt(){if(confirm(`Reboot ALL registered phones?`))try{let e=h();(await fetch(`${e}/api/phones/reboot`,{method:`POST`,headers:g()})).ok?b(`Reboot command sent to all phones`,`success`):b(`Failed to reboot phones`,`error`)}catch(e){console.error(`Error rebooting all phones:`,e),b(`Failed to reboot phones`,`error`)}}function Ot(){let e=document.getElementById(`add-extension-form`);e&&e.addEventListener(`submit`,async e=>{e.preventDefault();let t=e=>document.getElementById(e)?.value??``,n=e=>document.getElementById(e)?.checked??!1,r={number:t(`new-ext-number`),name:t(`new-ext-name`),email:t(`new-ext-email`),password:t(`new-ext-password`),voicemail_pin:t(`new-ext-voicemail-pin`),did_number:t(`new-ext-did-number`),allow_external:n(`new-ext-allow-external`),is_admin:n(`new-ext-is-admin`)};try{let e=h(),t=await fetch(`${e}/api/extensions`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(r)}),n=await t.json();t.ok&&n.success?(b(`Extension added successfully`,`success`),St(),bt()):b(n.error||`Failed to add extension`,`error`)}catch(e){console.error(`Error adding extension:`,e),b(`Failed to add extension`,`error`)}});let t=document.getElementById(`edit-extension-form`);t&&t.addEventListener(`submit`,async e=>{e.preventDefault();let t=e=>document.getElementById(e)?.value??``,n=e=>document.getElementById(e)?.checked??!1,r=t(`edit-ext-number`),i=t(`edit-ext-password`),a=t(`edit-ext-voicemail-pin`),o={name:t(`edit-ext-name`),email:t(`edit-ext-email`),did_number:t(`edit-ext-did-number`),allow_external:n(`edit-ext-allow-external`),is_admin:n(`edit-ext-is-admin`)};i&&(o.password=i),a&&(o.voicemail_pin=a);try{let e=h(),t=await fetch(`${e}/api/extensions/${r}`,{method:`PUT`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(o)}),n=await t.json();t.ok&&n.success?(b(`Extension updated successfully`,`success`),wt(),bt()):b(n.error||`Failed to update extension`,`error`)}catch(e){console.error(`Error updating extension:`,e),b(`Failed to update extension`,`error`)}})}window.loadExtensions=bt,window.showAddExtensionModal=xt,window.closeAddExtensionModal=St,window.editExtension=Ct,window.closeEditExtensionModal=wt,window.deleteExtension=Tt,window.rebootPhone=Et,window.rebootAllPhones=Dt,document.readyState===`loading`?document.addEventListener(`DOMContentLoaded`,Ot):Ot();async function kt(){try{let e=h(),t=await fetch(`${e}/api/extensions`,{headers:g()});if(!t.ok)throw Error(`HTTP error! status: ${t.status}`);let n=await t.json(),r=document.getElementById(`vm-extension-select`);if(!r)return;r.innerHTML=`<option value="">Select Extension</option>`;for(let e of n){let t=document.createElement(`option`);t.value=e.number,t.textContent=`${e.number} - ${e.name}`,r.appendChild(t)}}catch(e){console.error(`Error loading voicemail tab:`,e),b(`Failed to load extensions`,`error`)}}async function At(){let e=document.getElementById(`vm-extension-select`)?.value;if(!e){for(let e of[`voicemail-pin-section`,`voicemail-messages-section`,`voicemail-box-overview`]){let t=document.getElementById(e);t&&(t.style.display=`none`)}return}for(let e of[`voicemail-pin-section`,`voicemail-messages-section`,`voicemail-box-overview`]){let t=document.getElementById(e);t&&(t.style.display=`block`)}let t=document.getElementById(`vm-current-extension`);t&&(t.textContent=e);try{let t=h(),n=await fetch(`${t}/api/voicemail/${e}`,{headers:g()});if(!n.ok)throw Error(`HTTP error! status: ${n.status}`);jt((await n.json()).messages,e)}catch(e){console.error(`Error loading voicemail:`,e),b(`Failed to load voicemail messages`,`error`)}}function jt(e,t){let n=document.getElementById(`voicemail-cards-view`);if(n){if(!e||e.length===0){n.innerHTML=`<div class="info-box">No voicemail messages</div>`;return}n.innerHTML=e.map(e=>{let n=new Date(e.timestamp).toLocaleString(),r=e.duration?`${e.duration}s`:`Unknown`,i=!e.listened;return`
            <div class="voicemail-card ${i?`unread`:``}">
                <div class="voicemail-card-header">
                    <div class="voicemail-from">${y(e.caller_id)}</div>
                    <span class="voicemail-status-badge ${i?`unread`:`read`}">
                        ${i?`NEW`:`READ`}
                    </span>
                </div>
                <div class="voicemail-card-body">
                    <div>Time: ${n}</div>
                    <div>Duration: ${r}</div>
                </div>
                <div class="voicemail-card-actions">
                    <button class="btn btn-primary btn-sm" onclick="playVoicemail('${t}', '${e.id}')">Play</button>
                    <button class="btn btn-secondary btn-sm" onclick="downloadVoicemail('${t}', '${e.id}')">Download</button>
                    <button class="btn btn-danger btn-sm" onclick="deleteVoicemail('${t}', '${e.id}')">Delete</button>
                </div>
            </div>
        `}).join(``)}}async function Mt(e,t){try{let n=`${h()}/api/voicemail/${e}/${t}/audio`,r=document.getElementById(`vm-audio-player`);r&&(r.src=n,r.play()),await Pt(e,t)}catch(e){console.error(`Error playing voicemail:`,e),b(`Failed to play voicemail`,`error`)}}async function Nt(e,t){let n=h();window.open(`${n}/api/voicemail/${e}/${t}/audio?download=1`,`_blank`)}async function Pt(e,t){try{let n=h();await fetch(`${n}/api/voicemail/${e}/${t}/read`,{method:`POST`,headers:g()})}catch(e){console.error(`Error marking voicemail read:`,e)}}async function Ft(e,t){if(confirm(`Delete this voicemail message?`))try{let n=h();(await fetch(`${n}/api/voicemail/${e}/${t}`,{method:`DELETE`,headers:g()})).ok?(b(`Voicemail deleted`,`success`),At()):b(`Failed to delete voicemail`,`error`)}catch(e){console.error(`Error deleting voicemail:`,e),b(`Failed to delete voicemail`,`error`)}}function It(){let e=document.getElementById(`vm-audio-player`);e&&(e.pause(),e.src=``);let t=document.getElementById(`voicemail-player-section`);t&&(t.style.display=`none`)}function Lt(){let e=document.getElementById(`voicemail-cards-view`),t=document.getElementById(`voicemail-table-view`),n=document.getElementById(`toggle-voicemail-view-btn`);if(e&&t){let r=e.style.display!==`none`;e.style.display=r?`none`:`block`,t.style.display=r?`block`:`none`,n&&(n.textContent=r?`Switch to Card View`:`Switch to Table View`)}}window.loadVoicemailTab=kt,window.loadVoicemailForExtension=At,window.playVoicemail=Mt,window.downloadVoicemail=Nt,window.deleteVoicemail=Ft,window.markVoicemailRead=Pt,window.closeVoicemailPlayer=It,window.toggleVoicemailView=Lt;async function Rt(){let e=document.getElementById(`calls-list`);if(e){e.innerHTML=`<div class="loading">Loading calls...</div>`;try{let t=await(await _(`${h()}/api/calls`,{headers:g()})).json();if(t.length===0){e.innerHTML=`<div class="loading">No active calls</div>`;return}e.innerHTML=t.map(e=>`
            <div class="call-item"><strong>Call:</strong> ${y(String(e))}</div>
        `).join(``)}catch(t){console.error(`Error loading calls:`,t),e.innerHTML=`<div class="loading">Error loading calls</div>`}}}async function zt(){try{let e=await _(`${h()}/api/config/codecs`,{headers:g()});if(!e.ok)throw Error(`Failed to load codecs (HTTP ${e.status})`);let t=await e.json(),n=document.getElementById(`codec-status`);n&&(t.codecs&&t.codecs.length>0?n.innerHTML=t.codecs.map(e=>`<div class="codec-item">${y(e.name)} - <span class="status-${e.enabled?`enabled`:`disabled`}">${e.enabled?`Enabled`:`Disabled`}</span></div>`).join(``):n.innerHTML=`<div class="info-box">No codecs configured</div>`)}catch(e){console.error(`Error loading codec status:`,e);let t=document.getElementById(`codec-status`);t&&(t.innerHTML=`<div class="error-box">Failed to load codec configuration</div>`)}}async function Bt(){try{let e=await _(`${h()}/api/config/dtmf`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}`);let t=await e.json(),n=document.getElementById(`dtmf-mode`);n&&(n.value=t.mode??`rfc2833`);let r=document.getElementById(`dtmf-detection-threshold`);r&&(r.value=String(t.threshold??-30))}catch(e){console.error(`Error loading DTMF config:`,e)}}async function Vt(){try{let e=h(),t={mode:document.getElementById(`dtmf-mode`)?.value??`rfc2833`,threshold:parseInt(document.getElementById(`dtmf-detection-threshold`)?.value??`-30`)};(await fetch(`${e}/api/config/dtmf`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(t)})).ok?b(`DTMF configuration saved`,`success`):b(`Failed to save DTMF configuration`,`error`)}catch(e){console.error(`Error saving DTMF config:`,e),b(`Failed to save DTMF configuration`,`error`)}}async function Ht(e){e&&e.preventDefault();try{let e=h(),t=document.getElementById(`codec-config-form`);if(!t)return;let n=new FormData(t),r=Object.fromEntries(n.entries());(await fetch(`${e}/api/config/codecs`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(r)})).ok?b(`Codec configuration saved`,`success`):b(`Failed to save codec configuration`,`error`)}catch(e){console.error(`Error saving codec config:`,e),b(`Failed to save codec configuration`,`error`)}}window.loadCalls=Rt,window.loadCodecStatus=zt,window.loadDTMFConfig=Bt,window.saveDTMFConfig=Vt,window.saveCodecConfig=Ht;var Ut=`Configuration saved successfully. Restart may be required for some changes.`;async function Wt(){try{let e=await _(`${h()}/api/config/full`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}`);let t=await e.json();if(t.features)for(let e of[`call-recording`,`call-transfer`,`call-hold`,`conference`,`voicemail`,`call-parking`,`call-queues`,`presence`,`music-on-hold`,`auto-attendant`]){let n=document.getElementById(`feature-${e}`),r=e.replace(/-/g,`_`);n&&(n.checked=t.features[r]??!1)}if(t.voicemail){let e=e=>document.getElementById(e);e(`voicemail-max-duration`)&&(e(`voicemail-max-duration`).value=String(t.voicemail.max_duration??120))}}catch(e){console.error(`Error loading config:`,e),b(`Failed to load configuration`,`error`)}}async function Gt(){try{let e=await _(`${h()}/api/config/features`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}`);let t=await e.json();Kt(`core-features-table`,t.core),Kt(`advanced-features-table`,t.advanced),Kt(`integration-features-table`,t.integrations)}catch(e){console.error(`Error loading features status:`,e),b(`Failed to load feature status`,`error`)}}function Kt(e,t){let n=document.getElementById(e);if(n){if(!t||Object.keys(t).length===0){n.innerHTML=`<tr><td colspan="3" style="text-align:center;">No features found</td></tr>`;return}n.innerHTML=Object.entries(t).map(([e,t])=>{let n=e.replace(/_/g,` `).replace(/\b\w/g,e=>e.toUpperCase()),r=t.enabled?`<span class="badge" style="background:#10b981;">Enabled</span>`:`<span class="badge" style="background:#6b7280;">Disabled</span>`;return`<tr>
            <td><strong>${y(n)}</strong></td>
            <td>${r}</td>
            <td>${y(t.description)}</td>
        </tr>`}).join(``)}}async function qt(e){try{let t=h(),n=document.getElementById(`${e}-form`);if(!n)return;let r=new FormData(n),i=Object.fromEntries(r.entries()),a=await fetch(`${t}/api/config/section`,{method:`PUT`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify({section:e,data:i})});a.ok?b(Ut,`success`):b((await a.json()).error||`Failed to save configuration`,`error`)}catch(t){console.error(`Error saving ${e} config:`,t),b(`Failed to save ${e} configuration`,`error`)}}async function Jt(){try{let e=await _(`${h()}/api/ssl/status`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}`);let t=await e.json(),n=document.getElementById(`ssl-status-info`);if(n&&(n.textContent=t.enabled?`Enabled`:`Disabled`,n.className=`status-badge ${t.enabled?`enabled`:`disabled`}`),t.certificate){let e=document.getElementById(`cert-details-section`);e&&(e.style.display=`block`);let n=document.getElementById(`cert-subject`),r=document.getElementById(`cert-issuer`),i=document.getElementById(`cert-valid-from`),a=document.getElementById(`cert-valid-until`);n&&(n.value=t.certificate.subject||`N/A`),r&&(r.value=t.certificate.issuer||`N/A`),i&&(i.value=t.certificate.expires||`N/A`),a&&(a.value=t.certificate.expires||`N/A`)}}catch(e){console.error(`Error loading SSL status:`,e)}}async function Yt(){try{let e=h(),t=await fetch(`${e}/api/ssl/generate-certificate`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify({})});t.ok?(b(`SSL certificate generated successfully. Server restart required.`,`success`),Jt()):b((await t.json()).error||`Failed to generate SSL certificate`,`error`)}catch(e){console.error(`Error generating SSL certificate:`,e),b(`Failed to generate SSL certificate`,`error`)}}async function Xt(){await Jt(),b(`SSL status refreshed`,`success`)}var Zt=[`features-config`,`voicemail-config`,`email-config`,`recording-config`,`security-config`,`advanced-features`,`conference-config`,`ssl-config`];function Qt(){for(let e of Zt){let t=document.getElementById(`${e}-form`);t&&t.addEventListener(`submit`,async t=>{t.preventDefault(),await qt(e)})}}window.loadConfig=Wt,window.loadFeaturesStatus=Gt,window.saveConfigSection=qt,window.loadSSLStatus=Jt,window.generateSSLCertificate=Yt,window.refreshSSLStatus=Xt,document.readyState===`loading`?document.addEventListener(`DOMContentLoaded`,Qt):Qt();var $t=[],C={};async function en(){await Promise.all([tn(),nn(),w(),rn(),an(),on()])}async function tn(){try{let e=h(),t=await fetch(`${e}/api/provisioning/vendors`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json();$t=n.vendors||[],C=n.models||{},cn(),ln()}catch(e){console.error(`Error loading vendors:`,e);let t=document.getElementById(`supported-vendors-list`);t&&(t.innerHTML=`<p>Failed to load supported vendors.</p>`)}}async function nn(){try{let e=h(),t=await fetch(`${e}/api/extensions`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=document.getElementById(`device-extension`);if(!r)return;r.innerHTML=`<option value="">Select Extension</option>`;for(let e of n){let t=document.createElement(`option`);t.value=e.number,t.textContent=`${e.number} - ${e.name}`,r.appendChild(t)}}catch(e){console.error(`Error loading extensions for provisioning:`,e)}}async function w(){try{let e=h(),t=await fetch(`${e}/api/provisioning/devices`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=document.getElementById(`provisioning-devices-table-body`);if(!r)return;let i=Array.isArray(n)?n:n.devices||[];if(i.length===0){r.innerHTML=`<tr><td colspan="8">No provisioned devices</td></tr>`;return}r.innerHTML=i.map(e=>`
            <tr>
                <td>${y(e.mac_address||``)}</td>
                <td>${y(e.extension_number||e.extension||``)}</td>
                <td>${y(e.device_type||`phone`)}</td>
                <td>${y(e.vendor||``)}</td>
                <td>${y(e.model||``)}</td>
                <td>${y(e.created_at||``)}</td>
                <td>${y(e.last_provisioned||`Never`)}</td>
                <td><button class="btn btn-danger btn-sm" onclick="deleteDevice('${y(e.mac_address||``)}')">Delete</button></td>
            </tr>
        `).join(``)}catch(e){console.error(`Error loading devices:`,e)}}async function rn(){try{let e=h(),t=await fetch(`${e}/api/provisioning/templates`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=document.getElementById(`templates-table-body`);if(!r)return;let i=n.templates||[];if(i.length===0){r.innerHTML=`<tr><td colspan="5">No templates configured</td></tr>`;return}r.innerHTML=i.map(e=>{let t=e.name||`${e.vendor||`unknown`}_${e.model||`unknown`}`;return`
            <tr>
                <td>${y(e.vendor||e.manufacturer||`Generic`)}</td>
                <td>${y(e.model||``)}</td>
                <td>${y(e.is_custom?`custom`:`built-in`)}</td>
                <td>${e.size?`${e.size} bytes`:`-`}</td>
                <td><button class="btn btn-sm btn-secondary" onclick="viewTemplate('${y(t)}')">View</button></td>
            </tr>`}).join(``)}catch(e){console.error(`Error loading templates:`,e)}}async function an(){try{let e=h(),t=await fetch(`${e}/api/provisioning/settings`,{headers:g()});if(!t.ok)return;let n=await t.json(),r=e=>document.getElementById(e);r(`provisioning-enabled`)&&(r(`provisioning-enabled`).checked=n.enabled??!1),r(`provisioning-url-format`)&&(r(`provisioning-url-format`).value=n.url_format??``),dn()}catch(e){console.error(`Error loading provisioning settings:`,e)}}async function on(){try{let e=h(),t=await fetch(`${e}/api/provisioning/phonebook-settings`,{headers:g()});if(!t.ok)return;let n=await t.json(),r=e=>document.getElementById(e);r(`ldap-phonebook-enabled`)&&(r(`ldap-phonebook-enabled`).checked=n.ldap_enabled??!1),r(`remote-phonebook-enabled`)&&(r(`remote-phonebook-enabled`).checked=n.remote_enabled??!1),pn(),mn()}catch(e){console.error(`Error loading phonebook settings:`,e)}}async function sn(e){if(confirm(`Delete device ${e}?`))try{let t=h();(await fetch(`${t}/api/provisioning/devices/${e}`,{method:`DELETE`,headers:g()})).ok?(b(`Device deleted`,`success`),w()):b(`Failed to delete device`,`error`)}catch(e){console.error(`Error deleting device:`,e),b(`Failed to delete device`,`error`)}}function cn(){let e=document.getElementById(`device-vendor`);if(e){e.innerHTML=`<option value="">Select Vendor</option>`;for(let t of $t){let n=document.createElement(`option`);n.value=t,n.textContent=t,e.appendChild(n)}}}function ln(){let e=document.getElementById(`supported-vendors-list`);if(e){if($t.length===0){e.innerHTML=`<p>No supported vendors found.</p>`;return}e.innerHTML=$t.map(e=>{let t=C[e]||C[e.toLowerCase()]||[],n=t.length>0?t.map(e=>`<span class="badge">${y(e.toUpperCase())}</span>`).join(` `):`<em>No models</em>`;return`<div style="margin-bottom: 8px;">
            <strong>${y(e.charAt(0).toUpperCase()+e.slice(1))}</strong>: ${n}
        </div>`}).join(``)}}function un(){let e=document.getElementById(`device-vendor`),t=document.getElementById(`device-model`);if(!e||!t)return;let n=e.value;if(t.innerHTML=``,!n){t.innerHTML=`<option value="">Select Vendor First</option>`;return}let r=C[n]||C[n.toLowerCase()]||[];if(r.length===0){t.innerHTML=`<option value="">No models available</option>`;return}t.innerHTML=`<option value="">Select Model</option>`;for(let e of r){let n=document.createElement(`option`);n.value=e,n.textContent=e,t.appendChild(n)}}function dn(){let e=document.getElementById(`provisioning-enabled`),t=document.getElementById(`provisioning-settings`);e&&t&&(t.style.display=e.checked?`block`:`none`)}async function fn(){try{let e=e=>document.getElementById(e)?.value??``,t={enabled:(e=>document.getElementById(e)?.checked??!1)(`provisioning-enabled`),server_ip:e(`provisioning-server-ip`),port:parseInt(e(`provisioning-port`),10)||9e3,custom_templates_dir:e(`provisioning-custom-dir`)},n=h();(await fetch(`${n}/api/provisioning/settings`,{method:`PUT`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(t)})).ok?b(`Provisioning settings saved`,`success`):b(`Failed to save provisioning settings`,`error`)}catch(e){console.error(`Error saving provisioning settings:`,e),b(`Failed to save provisioning settings`,`error`)}}function pn(){let e=document.getElementById(`ldap-phonebook-enabled`),t=document.getElementById(`ldap-phonebook-settings`);e&&t&&(t.style.display=e.checked?`block`:`none`)}function mn(){let e=document.getElementById(`remote-phonebook-enabled`),t=document.getElementById(`remote-phonebook-settings`);e&&t&&(t.style.display=e.checked?`block`:`none`)}async function hn(){try{let e=e=>document.getElementById(e)?.value??``,t=e=>document.getElementById(e)?.checked??!1,n={ldap_enabled:t(`ldap-phonebook-enabled`),ldap_server:e(`ldap-phonebook-server`),ldap_port:parseInt(e(`ldap-phonebook-port`),10)||636,ldap_base_dn:e(`ldap-phonebook-base`),ldap_bind_user:e(`ldap-phonebook-user`),ldap_bind_password:e(`ldap-phonebook-password`),ldap_use_tls:t(`ldap-phonebook-tls`),ldap_display_name:e(`ldap-phonebook-display-name`),remote_enabled:t(`remote-phonebook-enabled`),remote_refresh_interval:parseInt(e(`remote-phonebook-refresh`),10)||60},r=h();(await fetch(`${r}/api/provisioning/phonebook-settings`,{method:`PUT`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(n)})).ok?b(`Phone book settings saved`,`success`):b(`Failed to save phone book settings`,`error`)}catch(e){console.error(`Error saving phonebook settings:`,e),b(`Failed to save phone book settings`,`error`)}}async function gn(){try{let e=h();(await fetch(`${e}/api/provisioning/reload-templates`,{method:`POST`,headers:g()})).ok?(b(`Templates reloaded from disk`,`success`),rn()):b(`Failed to reload templates`,`error`)}catch(e){console.error(`Error reloading templates:`,e),b(`Failed to reload templates`,`error`)}}function _n(){let e=document.getElementById(`add-device-form`);e&&e.reset();let t=document.getElementById(`device-model`);t&&(t.innerHTML=`<option value="">Select Vendor First</option>`)}function vn(e){b(`Viewing template: ${e}`,`info`)}function yn(){let e=document.getElementById(`add-device-form`);e&&e.addEventListener(`submit`,async e=>{e.preventDefault();let t=e=>document.getElementById(e)?.value??``,n={mac_address:t(`device-mac`),extension_number:t(`device-extension`),vendor:t(`device-vendor`),model:t(`device-model`)};if(!n.mac_address||!n.extension_number||!n.vendor||!n.model){b(`Please fill in all required fields`,`error`);return}try{let e=h(),t=await fetch(`${e}/api/provisioning/devices`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(n)}),r=await t.json();t.ok&&r.success?(b(r.message||`Device added successfully`,`success`),_n(),w()):b(r.error||`Failed to add device`,`error`)}catch(e){console.error(`Error adding device:`,e),b(`Failed to add device`,`error`)}})}window.loadProvisioning=en,window.loadSupportedVendors=tn,window.loadProvisioningDevices=w,window.loadProvisioningTemplates=rn,window.loadProvisioningSettings=an,window.loadPhonebookSettings=on,window.deleteDevice=sn,window.viewTemplate=vn,window.updateModelOptions=un,window.toggleProvisioningEnabled=dn,window.saveProvisioningSettings=fn,window.toggleLdapPhonebookSettings=pn,window.toggleRemotePhonebookSettings=mn,window.savePhonebookSettings=hn,window.reloadTemplates=gn,window.resetAddDeviceForm=_n,document.readyState===`loading`?document.addEventListener(`DOMContentLoaded`,yn):yn();async function bn(){let e=document.getElementById(`registered-phones-table-body`);if(e)try{let t=await _(`${h()}/api/registered-phones`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json();if(n.length===0){e.innerHTML=`<tr><td colspan="5">No registered phones</td></tr>`;return}e.innerHTML=n.map(e=>`
            <tr>
                <td>${y(e.extension_number||``)}</td>
                <td>${y(e.ip_address||``)}</td>
                <td>${y(e.mac_address||``)}</td>
                <td>${y(e.user_agent||``)}</td>
                <td>${y(e.last_registered||``)}</td>
            </tr>
        `).join(``)}catch(t){console.error(`Error loading registered phones:`,t),e.innerHTML=`<tr><td colspan="5">Error loading phones</td></tr>`}}async function xn(){let e=document.getElementById(`registered-atas-table-body`);if(e)try{let t=await _(`${h()}/api/registered-phones/atas`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json();if(n.length===0){e.innerHTML=`<tr><td colspan="6">No registered ATAs</td></tr>`;return}e.innerHTML=n.map(e=>{let t=[e.vendor,e.model].filter(Boolean).join(` `);return`
            <tr>
                <td>${y(e.extension_number||``)}</td>
                <td>${y(e.ip_address||``)}</td>
                <td>${y(e.mac_address||``)}</td>
                <td>${y(t)}</td>
                <td>${y(e.user_agent||``)}</td>
                <td>${y(e.last_registered||``)}</td>
            </tr>
        `}).join(``)}catch(t){console.error(`Error loading ATAs:`,t),e.innerHTML=`<tr><td colspan="6">Error loading ATAs</td></tr>`}}window.loadRegisteredPhones=bn,window.loadRegisteredATAs=xn;async function Sn(){try{let e=h(),t=await fetch(`${e}/api/fraud-detection/alerts`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=document.getElementById(`fraud-alerts-list`);if(!r)return;let i=n.alerts??[];if(i.length===0){r.innerHTML=`<div class="info-box">No fraud alerts</div>`;return}r.innerHTML=i.map(e=>`
            <div class="alert-item ${e.severity||`info`}">
                <strong>${y(e.type||`Alert`)}</strong> - ${y(e.description||``)}
                <span class="alert-time">${new Date(e.timestamp).toLocaleString()}</span>
            </div>
        `).join(``)}catch(e){console.error(`Error loading fraud alerts:`,e)}}async function Cn(){try{let e=h(),t=await fetch(`${e}/api/callback-queue/list`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=document.getElementById(`callback-list`);if(!r)return;let i=n.queue??[];if(i.length===0){r.innerHTML=`<tr><td colspan="5">No callbacks in queue</td></tr>`;return}r.innerHTML=i.map(e=>`
            <tr>
                <td>${y(e.caller||``)}</td>
                <td>${y(e.number||``)}</td>
                <td>${y(e.status||``)}</td>
                <td>${new Date(e.requested_at).toLocaleString()}</td>
                <td>
                    <button class="btn btn-primary btn-sm" onclick="startCallback('${e.id}')">Start</button>
                    <button class="btn btn-danger btn-sm" onclick="cancelCallback('${e.id}')">Cancel</button>
                </td>
            </tr>
        `).join(``)}catch(e){console.error(`Error loading callback queue:`,e)}}async function wn(e){try{let t=h();(await fetch(`${t}/api/callback-queue/start`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify({callback_id:e,agent_id:`admin`})})).ok?(b(`Callback initiated`,`success`),Cn()):b(`Failed to start callback`,`error`)}catch(e){console.error(`Error starting callback:`,e),b(`Failed to start callback`,`error`)}}async function Tn(e){try{let t=h();(await fetch(`${t}/api/callback-queue/cancel`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify({callback_id:e})})).ok?(b(`Callback cancelled`,`success`),Cn()):b(`Failed to cancel callback`,`error`)}catch(e){console.error(`Error cancelling callback:`,e),b(`Failed to cancel callback`,`error`)}}async function En(){try{let e=h(),t=await fetch(`${e}/api/mobile-push/devices`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=document.getElementById(`mobile-devices-list`);if(!r)return;let i=n.devices??[];if(i.length===0){r.innerHTML=`<div class="info-box">No registered devices</div>`;return}r.innerHTML=i.map(e=>`
            <div class="device-item">
                <strong>${y(e.name||e.device_id)}</strong> - ${y(e.platform||`Unknown`)}
                <span class="status-badge ${e.active?`enabled`:`disabled`}">${e.active?`Active`:`Inactive`}</span>
            </div>
        `).join(``)}catch(e){console.error(`Error loading mobile push devices:`,e)}}async function Dn(){try{let e=h(),t=await fetch(`${e}/api/framework/speech-analytics/configs`,{headers:g()});if(!t.ok)return;let n=await t.json(),r=document.getElementById(`speech-analytics-configs-table`);if(r){let e=n.configs??[];e.length===0?r.innerHTML=`<div class="info-box">No speech analytics configurations</div>`:r.innerHTML=`
                    <table class="table">
                        <thead>
                            <tr>
                                <th>Configuration</th>
                                <th>Status</th>
                                <th>Details</th>
                            </tr>
                        </thead>
                        <tbody>
                            ${e.map(e=>`
                                <tr>
                                    <td>${y(e.name||`N/A`)}</td>
                                    <td>${y(e.status||`inactive`)}</td>
                                    <td><small>${y(JSON.stringify(e))}</small></td>
                                </tr>
                            `).join(``)}
                        </tbody>
                    </table>
                `}}catch(e){console.error(`Error loading speech analytics:`,e)}}window.loadFraudAlerts=Sn,window.loadCallbackQueue=Cn,window.startCallback=wn,window.cancelCallback=Tn,window.loadMobilePushDevices=En,window.loadSpeechAnalyticsConfigs=Dn;async function On(){try{let e=h(),t=await fetch(`${e}/api/emergency/contacts`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=document.getElementById(`emergency-contacts-table`);if(!r)return;let i=n.contacts??[];if(i.length===0){r.innerHTML=`<tr><td colspan="5">No emergency contacts</td></tr>`;return}r.innerHTML=i.map(e=>`
            <tr>
                <td>${y(e.name||``)}</td>
                <td>${y(e.phone||``)}</td>
                <td>${y(e.role||``)}</td>
                <td>${kn(e.priority)}</td>
                <td><button class="btn btn-danger btn-sm" onclick="deleteEmergencyContact('${e.id}')">Delete</button></td>
            </tr>
        `).join(``)}catch(e){console.error(`Error loading emergency contacts:`,e)}}function kn(e){return`<span class="status-badge ${{high:`danger`,medium:`warning`,low:`info`}[e??``]??`info`}">${e??`normal`}</span>`}async function An(){try{let e=h(),t=await fetch(`${e}/api/emergency/history`,{headers:g()});if(!t.ok)return;let n=await t.json(),r=document.getElementById(`emergency-history-table`);if(!r)return;let i=n.history??[];r.innerHTML=i.length===0?`<div class="info-box">No emergency history</div>`:i.map(e=>`
                <div class="history-item">
                    <strong>${new Date(e.timestamp).toLocaleString()}</strong> - ${y(e.description||``)}
                </div>
            `).join(``)}catch(e){console.error(`Error loading emergency history:`,e)}}async function jn(e){if(confirm(`Delete this emergency contact?`))try{let t=h();(await fetch(`${t}/api/emergency/contacts/${e}`,{method:`DELETE`,headers:g()})).ok&&(b(`Emergency contact deleted`,`success`),On())}catch(e){console.error(`Error deleting contact:`,e),b(`Failed to delete contact`,`error`)}}async function Mn(){try{let e=h(),t=await fetch(`${e}/api/framework/nomadic-e911/sites`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=document.getElementById(`e911-sites-table`);if(!r)return;let i=n.sites??[];r.innerHTML=i.length===0?`<div class="info-box">No E911 sites configured</div>`:i.map(e=>`
                <div class="site-item">
                    <strong>${y(e.name||``)}</strong> - ${y(e.address||``)}
                    <button class="btn btn-sm btn-secondary" onclick="editE911Site('${e.id}')">Edit</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteE911Site('${e.id}')">Delete</button>
                </div>
            `).join(``)}catch(e){console.error(`Error loading E911 sites:`,e);let t=document.getElementById(`e911-sites-table`);t&&(t.innerHTML=`<div class="error-box">Failed to load E911 sites</div>`)}}async function Nn(){try{let e=h(),t=await fetch(`${e}/api/framework/nomadic-e911/locations`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=document.getElementById(`extension-locations-table`);if(r){let e=n.locations??[];r.innerHTML=e.length===0?`<div class="info-box">No locations assigned</div>`:e.map(e=>`
                    <div class="location-item">
                        Extension ${y(e.extension)} - ${y(e.site_name||`Unassigned`)}
                    </div>
                `).join(``)}}catch(e){console.error(`Error loading extension locations:`,e)}}function Pn(){let e=document.getElementById(`add-emergency-contact-modal`);e&&e.classList.add(`active`)}function Fn(){let e=document.getElementById(`add-emergency-contact-modal`);e&&e.classList.remove(`active`)}function In(){let e=document.getElementById(`trigger-emergency-modal`);e&&e.classList.add(`active`)}function Ln(){let e=document.getElementById(`trigger-emergency-modal`);e&&e.classList.remove(`active`)}async function Rn(e){e.preventDefault();let t=e=>document.getElementById(e)?.value??``,n=e=>document.getElementById(e)?.checked??!1,r=[];n(`method-call`)&&r.push(`call`),n(`method-page`)&&r.push(`page`),n(`method-email`)&&r.push(`email`),n(`method-sms`)&&r.push(`sms`);let i={name:t(`emergency-contact-name`),extension:t(`emergency-contact-extension`),phone:t(`emergency-contact-phone`),email:t(`emergency-contact-email`),priority:t(`emergency-contact-priority`),notification_methods:r};try{let e=h();(await fetch(`${e}/api/emergency/contacts`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(i)})).ok?(b(`Emergency contact added`,`success`),Fn(),On()):b(`Failed to add emergency contact`,`error`)}catch(e){console.error(`Error adding emergency contact:`,e),b(`Failed to add emergency contact`,`error`)}}async function zn(e){e.preventDefault();let t=e=>document.getElementById(e)?.value??``,n={type:t(`trigger-type`),details:t(`trigger-details`),info:t(`trigger-info`)};try{let e=h();(await fetch(`${e}/api/emergency/trigger`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(n)})).ok?(b(`Emergency notification sent`,`success`),Ln(),An()):b(`Failed to trigger emergency`,`error`)}catch(e){console.error(`Error triggering emergency:`,e),b(`Failed to trigger emergency`,`error`)}}async function Bn(){try{let e=h();(await fetch(`${e}/api/emergency/test`,{method:`GET`,headers:g()})).ok?b(`Test notification sent successfully`,`success`):b(`Failed to send test notification`,`error`)}catch(e){console.error(`Error testing emergency notification:`,e),b(`Failed to send test notification`,`error`)}}window.loadEmergencyContacts=On,window.loadEmergencyHistory=An,window.deleteEmergencyContact=jn,window.loadE911Sites=Mn,window.loadExtensionLocations=Nn,window.showAddEmergencyContactModal=Pn,window.closeAddEmergencyContactModal=Fn,window.showTriggerEmergencyModal=In,window.closeTriggerEmergencyModal=Ln,window.addEmergencyContact=Rn,window.triggerEmergency=zn,window.testEmergencyNotification=Bn;async function Vn(){try{let e=h(),t=await fetch(`${e}/api/phone-book`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=document.getElementById(`phone-book-body`);if(!r)return;let i=n.entries??[];if(i.length===0){r.innerHTML=`<tr><td colspan="5">No phone book entries</td></tr>`;return}r.innerHTML=i.map(e=>`
            <tr>
                <td>${y(e.name||``)}</td>
                <td>${y(e.number||``)}</td>
                <td>${y(e.email||``)}</td>
                <td>${y(e.group||`General`)}</td>
                <td>
                    <button class="btn btn-primary btn-sm" onclick="editPhoneBookEntry('${e.id}')">Edit</button>
                    <button class="btn btn-danger btn-sm" onclick="deletePhoneBookEntry('${e.id}')">Delete</button>
                </td>
            </tr>
        `).join(``)}catch(e){console.error(`Error loading phone book:`,e)}}async function Hn(e){if(confirm(`Delete this phone book entry?`))try{let t=h();(await fetch(`${t}/api/phone-book/${e}`,{method:`DELETE`,headers:g()})).ok&&(b(`Phone book entry deleted`,`success`),Vn())}catch(e){console.error(`Error deleting entry:`,e),b(`Failed to delete entry`,`error`)}}window.loadPhoneBook=Vn,window.deletePhoneBookEntry=Hn;async function Un(){await Promise.all([T(),Wn(),Gn()])}async function T(){try{let e=h(),t=await fetch(`${e}/api/paging/zones`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=document.getElementById(`paging-zones-table-body`);if(!r)return;let i=n.zones??[];if(i.length===0){r.innerHTML=`<tr><td colspan="4">No paging zones</td></tr>`;return}r.innerHTML=i.map(e=>`
            <tr>
                <td>${y(e.name||``)}</td>
                <td>${y(e.number||``)}</td>
                <td>${e.devices?.length||0} devices</td>
                <td><button class="btn btn-danger btn-sm" onclick="deletePagingZone('${e.id}')">Delete</button></td>
            </tr>
        `).join(``)}catch(e){console.error(`Error loading paging zones:`,e)}}async function Wn(){try{let e=h(),t=await fetch(`${e}/api/paging/devices`,{headers:g()});if(!t.ok)return;let n=await t.json(),r=document.getElementById(`paging-devices-table-body`);if(r){let e=n.devices??[];r.innerHTML=e.length===0?`<div class="info-box">No paging devices</div>`:e.map(e=>`<div class="device-item">${y(e.name||e.id)}</div>`).join(``)}}catch(e){console.error(`Error loading paging devices:`,e)}}async function Gn(){try{let e=h(),t=await fetch(`${e}/api/paging/active`,{headers:g()});if(!t.ok)return;let n=await t.json(),r=document.getElementById(`active-pages-table-body`);if(r){let e=n.pages??[];r.innerHTML=e.length===0?`<div class="info-box">No active pages</div>`:e.map(e=>`<div class="page-item">${y(e.zone)} - ${y(e.initiator)}</div>`).join(``)}}catch(e){console.error(`Error loading active pages:`,e)}}async function Kn(e){if(confirm(`Delete this paging zone?`))try{let t=h();(await fetch(`${t}/api/paging/zones/${e}`,{method:`DELETE`,headers:g()})).ok&&(b(`Paging zone deleted`,`success`),T())}catch(e){console.error(`Error deleting paging zone:`,e),b(`Failed to delete zone`,`error`)}}async function qn(){let e=prompt(`Zone Extension (e.g., 701):`);if(!e)return;let t=prompt(`Zone Name (e.g., "Warehouse"):`);if(!t)return;let n={extension:e,name:t,description:prompt(`Description (optional):`)??``,device_id:prompt(`Device ID (optional):`)??``};try{let e=h(),r=await(await fetch(`${e}/api/paging/zones`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(n)})).json();r.success?(b(`Zone ${t} added successfully`,`success`),T()):b(r.message??`Failed to add zone`,`error`)}catch(e){console.error(`Error adding zone ${t}:`,e),b(`Error adding zone ${t}`,`error`)}}async function Jn(){let e=prompt(`Device ID (e.g., "dac-1"):`);if(!e)return;let t=prompt(`Device Name (e.g., "Main PA System"):`);if(!t)return;let n={device_id:e,name:t,type:prompt(`Device Type (e.g., "sip_gateway"):`)??`sip_gateway`,sip_address:prompt(`SIP Address (e.g., "paging@192.168.1.10:5060"):`)??``};try{let e=h(),r=await(await fetch(`${e}/api/paging/devices`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(n)})).json();r.success?(b(`Device ${t} added successfully`,`success`),Wn()):b(r.message??`Failed to add device`,`error`)}catch(e){console.error(`Error adding device ${t}:`,e),b(`Error adding device ${t}`,`error`)}}async function Yn(e){if(confirm(`Delete paging device ${e}?`))try{let t=h(),n=await(await fetch(`${t}/api/paging/devices/${e}`,{method:`DELETE`,headers:g()})).json();n.success?(b(`Device ${e} deleted`,`success`),Wn()):b(n.message??`Failed to delete device`,`error`)}catch(e){console.error(`Error deleting device:`,e),b(`Error deleting device`,`error`)}}window.loadPagingData=Un,window.loadPagingZones=T,window.loadPagingDevices=Wn,window.loadActivePages=Gn,window.deletePagingZone=Kn,window.showAddZoneModal=qn,window.showAddDeviceModal=Jn,window.deletePagingDevice=Yn;async function E(){await Promise.all([Xn(),Zn()])}async function Xn(){try{let e=h(),t=await fetch(`${e}/api/queues`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=document.getElementById(`queues-table-body`);if(!r)return;let i=n.queues??[];if(i.length===0){r.innerHTML=`<tr><td colspan="9">No call queues configured</td></tr>`;return}r.innerHTML=i.map(e=>`
            <tr>
                <td>${y(e.queue_number)}</td>
                <td>${y(e.name)}</td>
                <td>${y(e.strategy)}</td>
                <td>${e.calls_waiting}${e.calls_waiting>0?` (${Math.round(e.longest_wait)}s)`:``}</td>
                <td>${e.available_agents}/${e.total_agents}</td>
                <td>${y(e.members.join(`, `)||`—`)}</td>
                <td>${y(e.fallback_mailbox)}</td>
                <td>${e.enabled?`✅`:`⛔`}</td>
                <td>
                    <button class="btn btn-secondary btn-sm" onclick="showEditQueueModal('${y(e.queue_number)}')">Edit</button>
                    <button class="btn btn-success btn-sm" onclick="showAddQueueAgentModal('${y(e.queue_number)}')">+ Agent</button>
                    <button class="btn btn-danger btn-sm" onclick="deleteQueue('${y(e.queue_number)}')">Delete</button>
                </td>
            </tr>
        `).join(``)}catch(e){console.error(`Error loading queues:`,e)}}async function Zn(){try{let e=h(),t=await fetch(`${e}/api/queues/agents/state`,{headers:g()});if(!t.ok)return;let n=await t.json(),r=document.getElementById(`queue-agents-table-body`);if(!r)return;let i=n.agents??[];if(i.length===0){r.innerHTML=`<tr><td colspan="7">No queue agents (add extensions to a queue first)</td></tr>`;return}r.innerHTML=i.map(e=>{let t=e.logged_in?e.paused?`Paused (${e.pause_reason??`manual`})`:`Available`:`Logged out`;return`
            <tr>
                <td>${y(e.extension)}</td>
                <td>${y(e.queues.join(`, `)||`—`)}</td>
                <td>${y(t)}</td>
                <td>${e.calls_taken}</td>
                <td>${e.consecutive_misses}</td>
                <td>${e.last_call_time?y(new Date(e.last_call_time).toLocaleString()):`—`}</td>
                <td>
                    <button class="btn btn-secondary btn-sm" onclick="setQueueAgentState('${y(e.extension)}', {logged_in: ${!e.logged_in}})">${e.logged_in?`Log out`:`Log in`}</button>
                    <button class="btn btn-secondary btn-sm" onclick="setQueueAgentState('${y(e.extension)}', {paused: ${!e.paused}})">${e.paused?`Unpause`:`Pause`}</button>
                </td>
            </tr>
        `}).join(``)}catch(e){console.error(`Error loading queue agents:`,e)}}async function Qn(e,t){try{let n=h(),r=await(await fetch(`${n}/api/queues/agents/${e}/state`,{method:`PUT`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(t)})).json();r.success?(b(`Agent ${e} updated`,`success`),E()):b(r.error??`Failed to update agent`,`error`)}catch(e){console.error(`Error updating agent state:`,e),b(`Error updating agent state`,`error`)}}async function $n(){let e=prompt(`Queue Number (e.g., 8001):`);if(!e)return;let t=prompt(`Queue Name (e.g., "Sales"):`);if(!t)return;let n=prompt(`Strategy (round_robin, least_recent, fewest_calls, random):`,`round_robin`)??`round_robin`;try{let r=h(),i=await(await fetch(`${r}/api/queues`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify({queue_number:e,name:t,strategy:n})})).json();i.success?(b(`Queue ${e} created`,`success`),E()):b(i.error??`Failed to create queue`,`error`)}catch(e){console.error(`Error creating queue:`,e),b(`Error creating queue`,`error`)}}async function er(e){let t=prompt(`Queue Name (leave blank to keep):`)??``,n=prompt(`Strategy (round_robin, least_recent, fewest_calls, random; blank to keep):`)??``,r=prompt(`Ring timeout seconds (blank to keep):`)??``,i=prompt(`Max wait seconds before voicemail (blank to keep):`)??``,a=prompt(`Fallback mailbox (blank to keep, "-" to reset to queue number):`)??``,o={};if(t&&(o.name=t),n&&(o.strategy=n),r&&(o.ring_timeout=parseInt(r,10)),i&&(o.max_wait_time=parseInt(i,10)),a===`-`?o.fallback_mailbox=null:a&&(o.fallback_mailbox=a),Object.keys(o).length!==0)try{let t=h(),n=await(await fetch(`${t}/api/queues/${e}`,{method:`PUT`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(o)})).json();n.success?(b(`Queue ${e} updated`,`success`),E()):b(n.error??`Failed to update queue`,`error`)}catch(e){console.error(`Error updating queue:`,e),b(`Error updating queue`,`error`)}}async function tr(e){if(confirm(`Delete queue ${e}?`))try{let t=h(),n=await(await fetch(`${t}/api/queues/${e}`,{method:`DELETE`,headers:g()})).json();n.success?(b(`Queue ${e} deleted`,`success`),E()):b(n.error??`Failed to delete queue`,`error`)}catch(e){console.error(`Error deleting queue:`,e),b(`Error deleting queue`,`error`)}}async function nr(e){let t=prompt(`Agent extension to add to queue ${e}:`);if(t)try{let n=h(),r=await(await fetch(`${n}/api/queues/${e}/agents`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify({extension:t})})).json();r.success?(b(`Agent ${t} added to ${e}`,`success`),E()):b(r.error??`Failed to add agent`,`error`)}catch(e){console.error(`Error adding agent:`,e),b(`Error adding agent`,`error`)}}async function rr(e,t){if(confirm(`Remove agent ${t} from queue ${e}?`))try{let n=h(),r=await(await fetch(`${n}/api/queues/${e}/agents/${t}`,{method:`DELETE`,headers:g()})).json();r.success?(b(`Agent ${t} removed from ${e}`,`success`),E()):b(r.error??`Failed to remove agent`,`error`)}catch(e){console.error(`Error removing agent:`,e),b(`Error removing agent`,`error`)}}window.loadQueuesData=E,window.loadQueues=Xn,window.loadQueueAgents=Zn,window.setQueueAgentState=Qn,window.showAddQueueModal=$n,window.showEditQueueModal=er,window.deleteQueue=tr,window.showAddQueueAgentModal=nr,window.removeQueueAgent=rr;async function D(){let e=document.getElementById(`license-status-container`);if(e){e.innerHTML=`<div class="loading">Loading license information...</div>`;try{let t=await _(`${h()}/api/license/status`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json();if(n.success&&n.license){let t=n.license,r=t.status===`active`||t.valid?`badge-success`:`badge-danger`,i=t.status||(t.valid?`Active`:`Invalid`);e.innerHTML=`
                <div class="ad-status-grid">
                    <div class="ad-status-item">
                        <strong>License Type</strong>
                        <span>${(t.type||`Unknown`).toUpperCase()}</span>
                    </div>
                    <div class="ad-status-item">
                        <strong>Status</strong>
                        <span class="badge ${r}">${i}</span>
                    </div>
                    <div class="ad-status-item">
                        <strong>Issued To</strong>
                        <span>${t.issued_to||`N/A`}</span>
                    </div>
                    <div class="ad-status-item">
                        <strong>Expires</strong>
                        <span>${t.expires_at||t.expiration||`Never`}</span>
                    </div>
                    <div class="ad-status-item">
                        <strong>Extensions</strong>
                        <span>${t.used_extensions??0} / ${t.max_extensions??`Unlimited`}</span>
                    </div>
                    <div class="ad-status-item">
                        <strong>Concurrent Calls</strong>
                        <span>${t.max_concurrent_calls??`Unlimited`}</span>
                    </div>
                    <div class="ad-status-item">
                        <strong>Licensing Enabled</strong>
                        <span class="badge ${t.licensing_enabled===!1?`badge-warning`:`badge-success`}">${t.licensing_enabled===!1?`No (Open Source Mode)`:`Yes`}</span>
                    </div>
                    ${t.key?`<div class="ad-status-item" style="grid-column: 1 / -1;">
                        <strong>License Key</strong>
                        <span style="font-family: monospace; font-size: 12px; word-break: break-all;">${t.key}</span>
                    </div>`:``}
                </div>
            `}else e.innerHTML=`<div class="info-box">No license installed. System is running in open-source mode with all features available.</div>`}catch(t){console.error(`Error loading license status:`,t),e.innerHTML=`<div class="error-message">Failed to load license status</div>`}}}async function O(){let e=document.getElementById(`license-features-container`);if(e){e.innerHTML=`<div class="loading">Loading features...</div>`;try{let t=await _(`${h()}/api/license/features`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json();if(!n.licensing_enabled){e.innerHTML=`<div class="info-box">Licensing disabled &mdash; all features are available (open-source mode).</div>`;return}let r=n.features??[],i=n.limits??{},a=`<p style="margin-bottom: 10px;"><strong>License Type:</strong> ${(n.license_type||`unknown`).toUpperCase()}</p>`;if(Object.keys(i).length>0){a+=`<h4 style="margin: 15px 0 8px;">Limits</h4><div class="ad-status-grid">`;for(let[e,t]of Object.entries(i))a+=`<div class="ad-status-item"><strong>${e.replace(/_/g,` `)}</strong><span>${t??`Unlimited`}</span></div>`;a+=`</div>`}if(r.length>0){a+=`<h4 style="margin: 15px 0 8px;">Included Features</h4><div style="display: flex; flex-wrap: wrap; gap: 8px;">`;for(let e of r)a+=`<span class="badge badge-success" style="padding: 4px 10px;">${e.replace(/_/g,` `)}</span>`;a+=`</div>`}e.innerHTML=a}catch(t){console.error(`Error loading license features:`,t),e.innerHTML=`<div class="error-message">Failed to load license features</div>`}}}async function ir(e){e&&e.preventDefault();let t=document.getElementById(`generate-license-result`),n=document.getElementById(`license-type`)?.value,r=document.getElementById(`issued-to`)?.value?.trim(),i=document.getElementById(`expiration-days`)?.value,a=document.getElementById(`max-extensions`)?.value,o=document.getElementById(`max-concurrent-calls`)?.value;if(!n||!r){b(`License type and organization/person are required`,`error`);return}let s={type:n,issued_to:r};i&&(s.expiration_days=parseInt(i,10)),a&&(s.max_extensions=parseInt(a,10)),o&&(s.max_concurrent_calls=parseInt(o,10));try{let e=await(await _(`${h()}/api/license/generate`,{method:`POST`,headers:g(),body:JSON.stringify(s)})).json();if(e.success&&e.license){b(`License generated successfully`,`success`);let n=JSON.stringify(e.license,null,2);t&&(t.innerHTML=`
                    <div class="config-section" style="margin-top: 10px; background: #e8f5e9; border: 1px solid #4caf50;">
                        <h4 style="margin-top: 0; color: #2e7d32;">Generated License</h4>
                        <pre style="background: #263238; color: #eeffff; padding: 15px; border-radius: 4px; overflow-x: auto; font-size: 12px; max-height: 300px;">${n}</pre>
                        <div class="action-buttons" style="margin-top: 10px;">
                            <button class="btn btn-primary" onclick="navigator.clipboard.writeText(document.getElementById('generated-license-json').textContent).then(()=>alert('Copied!'))">📋 Copy to Clipboard</button>
                            <button class="btn btn-success" onclick="autoInstallGeneratedLicense()">📥 Install This License</button>
                        </div>
                        <pre id="generated-license-json" style="display:none;">${n}</pre>
                    </div>
                `)}else b(e.error||`Failed to generate license`,`error`),t&&(t.innerHTML=`<div class="error-message">${e.error||`Failed to generate license`}</div>`)}catch(e){console.error(`Error generating license:`,e),b(`Failed to generate license`,`error`),t&&(t.innerHTML=`<div class="error-message">Failed to generate license</div>`)}}async function ar(){let e=document.getElementById(`generated-license-json`);if(e)try{await sr(JSON.parse(e.textContent||`{}`),!1)}catch(e){console.error(`Error auto-installing license:`,e),b(`Failed to install generated license`,`error`)}}async function or(e){e&&e.preventDefault();let t=document.getElementById(`license-data`),n=document.getElementById(`enforce-licensing`),r=document.getElementById(`install-license-result`);if(!t||!t.value.trim()){b(`Please enter license data (JSON)`,`error`);return}let i;try{i=JSON.parse(t.value.trim())}catch{b(`Invalid JSON format. Please check the license data.`,`error`);return}let a=n?.checked??!1;try{await sr(i,a),t.value=``,n&&(n.checked=!1)}catch(e){console.error(`Error installing license:`,e),b(`Failed to install license`,`error`),r&&(r.innerHTML=`<div class="error-message">Failed to install license</div>`)}}async function sr(e,t){let n=document.getElementById(`install-license-result`),r=await(await _(`${h()}/api/license/install`,{method:`POST`,headers:g(),body:JSON.stringify({license_data:e,enforce_licensing:t})})).json();r.success?(b(r.message||`License installed successfully`,`success`),n&&(n.innerHTML=`<div class="info-box" style="border-left-color: #4caf50;">${r.message||`License installed successfully`}</div>`),D(),O()):(b(r.error||`Failed to install license`,`error`),n&&(n.innerHTML=`<div class="error-message">${r.error||`Failed to install license`}</div>`))}async function cr(e){let t=document.getElementById(`licensing-toggle-result`),n=e?`enable`:`disable`;if(confirm(`Are you sure you want to ${n} licensing?`))try{let r=await(await _(`${h()}/api/license/toggle`,{method:`POST`,headers:g(),body:JSON.stringify({enabled:e})})).json();r.success?(b(r.message||`Licensing ${n}d successfully`,`success`),t&&(t.innerHTML=`<div class="info-box" style="border-left-color: #4caf50;">${r.message||`Licensing ${n}d successfully`}</div>`),D(),O()):(b(r.error||`Failed to ${n} licensing`,`error`),t&&(t.innerHTML=`<div class="error-message">${r.error||`Failed to ${n} licensing`}</div>`))}catch(e){console.error(`Error toggling licensing:`,e),b(`Failed to ${n} licensing`,`error`),t&&(t.innerHTML=`<div class="error-message">Failed to ${n} licensing</div>`)}}async function lr(){if(!confirm(`Are you sure you want to revoke the current license? This action cannot be undone.`))return;let e=document.getElementById(`revoke-license-result`);try{let t=await(await _(`${h()}/api/license/revoke`,{method:`POST`,headers:g()})).json();t.success?(b(`License revoked successfully`,`success`),e&&(e.innerHTML=`<div class="info-box" style="border-left-color: #4caf50;">License revoked successfully</div>`),D(),O()):(b(t.error||`Failed to revoke license`,`error`),e&&(e.innerHTML=`<div class="error-message">${t.error||`Failed to revoke license`}</div>`))}catch(t){console.error(`Error revoking license:`,t),b(`Failed to revoke license`,`error`),e&&(e.innerHTML=`<div class="error-message">Failed to revoke license</div>`)}}async function ur(){if(!confirm(`Are you sure you want to remove the license lock? This will allow licensing to be disabled.`))return;let e=document.getElementById(`remove-lock-result`);try{let t=await(await _(`${h()}/api/license/remove_lock`,{method:`POST`,headers:g()})).json();t.success?(b(t.message||`License lock removed`,`success`),e&&(e.innerHTML=`<div class="info-box" style="border-left-color: #4caf50;">${t.message||`License lock removed`}</div>`)):(b(t.error||`Failed to remove license lock`,`error`),e&&(e.innerHTML=`<div class="error-message">${t.error||`Failed to remove license lock`}</div>`))}catch(t){console.error(`Error removing license lock:`,t),b(`Failed to remove license lock`,`error`),e&&(e.innerHTML=`<div class="error-message">Failed to remove license lock</div>`)}}function dr(){D(),O()}window.loadLicenseStatus=D,window.loadLicenseFeatures=O,window.generateLicense=ir,window.autoInstallGeneratedLicense=ar,window.installLicense=or,window.toggleLicensing=cr,window.revokeLicense=lr,window.removeLicenseLock=ur,window.initLicenseManagement=dr;var k={};function fr(){return typeof Chart<`u`}async function pr(){try{let e=document.getElementById(`analytics-period`)?.value??`7`,t=h(),n=await fetch(`${t}/api/analytics/overview?days=${e}`,{headers:g()});if(!n.ok)throw Error(`HTTP ${n.status}`);let r=await n.json();mr(r),hr(r.top_callers??[]),fr()&&(r.daily_trends&&gr(r.daily_trends),r.hourly_distribution&&_r(r.hourly_distribution),r.disposition&&vr(r.disposition))}catch(e){console.error(`Error loading analytics:`,e)}}function mr(e){let t=e=>document.getElementById(e),n=t(`analytics-total-calls`),r=t(`analytics-avg-duration`),i=t(`analytics-answer-rate`),a=t(`analytics-answered-calls`);n&&(n.textContent=String(e.total_calls??0)),r&&(r.textContent=`${e.avg_duration??0}s`),i&&(i.textContent=`${e.answer_rate??0}%`),a&&(a.textContent=String(e.answered_calls??0))}function hr(e){let t=document.getElementById(`top-callers-table`);if(t){if(e.length===0){t.innerHTML=`<tr><td colspan="4" class="loading">No call data available</td></tr>`;return}t.innerHTML=e.map(e=>`<tr>
            <td>${y(String(e.extension??`Unknown`))}</td>
            <td>${e.calls??0}</td>
            <td>${((e.total_duration??0)/60).toFixed(1)}</td>
            <td>${(e.avg_duration??0).toFixed(1)}</td>
        </tr>`).join(``)}}function gr(e){let t=document.getElementById(`daily-trends-chart`)?.getContext(`2d`);t&&(k.dailyTrends&&k.dailyTrends.destroy(),k.dailyTrends=new Chart(t,{type:`line`,data:{labels:e.labels||[],datasets:[{label:`Calls`,data:e.data||[],borderColor:`#3b82f6`,tension:.3,fill:!1}]},options:{responsive:!0,maintainAspectRatio:!1}}))}function _r(e){let t=document.getElementById(`hourly-distribution-chart`)?.getContext(`2d`);t&&(k.hourlyDist&&k.hourlyDist.destroy(),k.hourlyDist=new Chart(t,{type:`bar`,data:{labels:e.labels||[],datasets:[{label:`Calls by Hour`,data:e.data||[],backgroundColor:`#60a5fa`}]},options:{responsive:!0,maintainAspectRatio:!1}}))}function vr(e){let t=document.getElementById(`disposition-chart`)?.getContext(`2d`);t&&(k.disposition&&k.disposition.destroy(),k.disposition=new Chart(t,{type:`doughnut`,data:{labels:e.labels||[],datasets:[{data:e.data||[],backgroundColor:[`#10b981`,`#ef4444`,`#f59e0b`,`#6b7280`]}]},options:{responsive:!0,maintainAspectRatio:!1}}))}async function yr(){try{let e=h(),t=await fetch(`${e}/api/qos/metrics`,{headers:g()});if(!t.ok)throw Error(`HTTP ${t.status}`);let n=await t.json(),r=n.active_calls??0,i=n.metrics??[],a=0,o=0;if(i.length>0){let e=i.map(e=>e.mos_score??0).filter(e=>e>0);a=e.length>0?e.reduce((e,t)=>e+t,0)/e.length:0,o=e.filter(e=>e<3.5).length}let s=document.getElementById(`qos-avg-mos`),c=document.getElementById(`qos-active-calls`),l=document.getElementById(`qos-total-calls`),u=document.getElementById(`qos-calls-with-issues`);s&&(s.textContent=a>0?a.toFixed(2):`N/A`),c&&(c.textContent=String(r)),l&&(l.textContent=String(i.length)),u&&(u.textContent=String(o))}catch(e){console.error(`Error loading QoS metrics:`,e)}}async function br(){try{let e=h();if((await fetch(`${e}/api/qos/clear-alerts`,{method:`POST`,headers:g()})).ok){let e=document.getElementById(`qos-alerts-container`);e&&(e.innerHTML=`<div class="info-box">No quality alerts</div>`),b(`QoS alerts cleared`,`success`)}else b(`Failed to clear QoS alerts`,`error`)}catch(e){console.error(`Error clearing QoS alerts:`,e),b(`Failed to clear QoS alerts`,`error`)}}async function xr(e){e&&e.preventDefault();try{let e=e=>document.getElementById(e)?.value??``,t={mos_min:parseFloat(e(`qos-threshold-mos`))||3.5,jitter_max:parseInt(e(`qos-threshold-jitter`),10)||50,packet_loss_max:parseFloat(e(`qos-threshold-loss`))||2,latency_max:parseInt(e(`qos-threshold-latency`),10)||300},n=h();(await fetch(`${n}/api/qos/thresholds`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(t)})).ok?b(`QoS thresholds saved`,`success`):b(`Failed to save QoS thresholds`,`error`)}catch(e){console.error(`Error saving QoS thresholds:`,e),b(`Failed to save QoS thresholds`,`error`)}}window.loadAnalytics=pr,window.loadQoSMetrics=yr,window.clearQoSAlerts=br,window.saveQoSThresholds=xr;function Sr(e){return{registered:`<span class="badge" style="background: #10b981;">Registered</span>`,unregistered:`<span class="badge" style="background: #6b7280;">Unregistered</span>`,failed:`<span class="badge" style="background: #ef4444;">Failed</span>`,disabled:`<span class="badge" style="background: #9ca3af;">Disabled</span>`,degraded:`<span class="badge" style="background: #f59e0b;">Degraded</span>`}[e]||e}function Cr(e){return{healthy:`<span class="badge" style="background: #10b981;">Healthy</span>`,warning:`<span class="badge" style="background: #f59e0b;">Warning</span>`,critical:`<span class="badge" style="background: #f59e0b;">Critical</span>`,down:`<span class="badge" style="background: #ef4444;">Down</span>`}[e]||e}var wr={0:`PCMU (G.711u)`,8:`PCMA (G.711a)`,9:`G.722`,18:`G.729`,2:`G.726-32`};function Tr(e){return!e||e.length===0?`—`:e.map(e=>wr[e]||e).join(`, `)}async function A(){try{let e=await _(`${h()}/api/sip-trunks`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}: ${e.statusText}`);let t=await e.json();if(t.trunks){let e=document.getElementById(`trunk-total`);e&&(e.textContent=String(t.count||0));let n=t.trunks.filter(e=>e.health_status===`healthy`).length,r=t.trunks.filter(e=>e.status===`registered`).length,i=t.trunks.reduce((e,t)=>e+t.channels_available,0),a=document.getElementById(`trunk-healthy`);a&&(a.textContent=String(n));let o=document.getElementById(`trunk-registered`);o&&(o.textContent=String(r));let s=document.getElementById(`trunk-total-channels`);s&&(s.textContent=String(i));let c=document.getElementById(`trunks-list`);if(!c)return;t.trunks.length===0?c.innerHTML=`<tr><td colspan="9" style="text-align: center;">No SIP trunks configured</td></tr>`:c.innerHTML=t.trunks.map(e=>{let t=Sr(e.status),n=Cr(e.health_status),r=(e.success_rate*100).toFixed(1);return`
                        <tr>
                            <td><strong>${y(e.name)}</strong><br/><small>${y(e.trunk_id)}</small></td>
                            <td>${y(e.host)}:${e.port}</td>
                            <td>${y(Tr(e.codec_preferences))}</td>
                            <td>${t}</td>
                            <td>${n}</td>
                            <td>${e.priority}</td>
                            <td>${e.channels_in_use}/${e.max_channels}</td>
                            <td>
                                <div style="display: flex; align-items: center; gap: 5px;">
                                    <div style="flex: 1; background: #e5e7eb; border-radius: 4px; height: 20px; overflow: hidden;">
                                        <div style="background: ${Number(r)>=95?`#10b981`:Number(r)>=80?`#f59e0b`:`#ef4444`}; height: 100%; width: ${r}%;"></div>
                                    </div>
                                    <span>${r}%</span>
                                </div>
                                <small>${e.successful_calls}/${e.total_calls} calls</small>
                            </td>
                            <td>
                                <button class="btn-small btn-primary" onclick="testTrunk('${y(e.trunk_id)}')">Test</button>
                                <button class="btn-small btn-danger" onclick="deleteTrunk('${y(e.trunk_id)}', '${y(e.name)}')">Delete</button>
                            </td>
                        </tr>
                    `}).join(``)}}catch(e){console.error(`Error loading SIP trunks:`,e),b(`Error loading SIP trunks: ${e instanceof Error?e.message:String(e)}`,`error`)}}async function Er(){try{let e=await _(`${h()}/api/sip-trunks/health`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}: ${e.statusText}`);let t=await e.json();if(t.health){let e=document.getElementById(`trunk-health-section`),n=document.getElementById(`trunk-health-container`);if(!e||!n)return;e.style.display=`block`,n.innerHTML=t.health.map(e=>`
                <div class="config-section" style="margin-bottom: 15px;">
                    <h4>${y(e.name)} (${y(e.trunk_id)})</h4>
                    <div class="stats-grid" style="grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));">
                        <div class="stat-card">
                            <div class="stat-value">${Cr(e.health_status)}</div>
                            <div class="stat-label">Health Status</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-value">${(e.success_rate*100).toFixed(1)}%</div>
                            <div class="stat-label">Success Rate</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-value">${e.consecutive_failures}</div>
                            <div class="stat-label">Consecutive Failures</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-value">${e.average_setup_time.toFixed(2)}s</div>
                            <div class="stat-label">Avg Setup Time</div>
                        </div>
                        <div class="stat-card">
                            <div class="stat-value">${e.failover_count}</div>
                            <div class="stat-label">Failover Count</div>
                        </div>
                    </div>
                    <div style="margin-top: 10px;">
                        <p><strong>Total Calls:</strong> ${e.total_calls} (${e.successful_calls} successful, ${e.failed_calls} failed)</p>
                        ${e.last_successful_call?`<p><strong>Last Success:</strong> ${new Date(e.last_successful_call).toLocaleString()}</p>`:``}
                        ${e.last_failed_call?`<p><strong>Last Failure:</strong> ${new Date(e.last_failed_call).toLocaleString()}</p>`:``}
                        ${e.last_health_check?`<p><strong>Last Check:</strong> ${new Date(e.last_health_check).toLocaleString()}</p>`:``}
                    </div>
                </div>
            `).join(``),b(`Health metrics loaded`,`success`)}}catch(e){console.error(`Error loading trunk health:`,e),b(`Error loading trunk health: ${e instanceof Error?e.message:String(e)}`,`error`)}}function Dr(){let e=document.getElementById(`add-trunk-modal`);e&&(e.style.display=`block`)}function Or(){let e=document.getElementById(`add-trunk-modal`);e&&(e.style.display=`none`);let t=document.getElementById(`add-trunk-form`);t&&t.reset()}async function kr(e){e.preventDefault();let t=Array.from(document.querySelectorAll(`input[name="trunk-codecs"]:checked`)).flatMap(e=>e.value.split(`,`)),n={trunk_id:document.getElementById(`trunk-id`).value,name:document.getElementById(`trunk-name`).value,host:document.getElementById(`trunk-host`).value,port:parseInt(document.getElementById(`trunk-port`).value),username:document.getElementById(`trunk-username`).value,password:document.getElementById(`trunk-password`).value,priority:parseInt(document.getElementById(`trunk-priority`).value),max_channels:parseInt(document.getElementById(`trunk-channels`).value),codec_preferences:t.length>0?t:[`0`,`8`,`18`]};try{let e=await(await _(`${h()}/api/sip-trunks`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(n)})).json();e.success?(b(`Trunk ${n.name} added successfully`,`success`),Or(),A()):b(e.error||`Error adding trunk`,`error`)}catch(e){console.error(`Error adding trunk:`,e),b(`Error adding trunk`,`error`)}}async function Ar(e,t){if(confirm(`Are you sure you want to delete trunk "${t}"?`))try{let n=await(await _(`${h()}/api/sip-trunks/${e}`,{method:`DELETE`,headers:g()})).json();n.success?(b(`Trunk ${t} deleted`,`success`),A()):b(n.error||`Error deleting trunk`,`error`)}catch(e){console.error(`Error deleting trunk:`,e),b(`Error deleting trunk`,`error`)}}async function jr(e){b(`Testing trunk...`,`info`);try{let t=await(await _(`${h()}/api/sip-trunks/test`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify({trunk_id:e})})).json();if(t.success){let e=t.health_status??`unknown`;b(`Trunk test complete: ${e}`,e===`healthy`?`success`:`warning`),A(),Er()}else b(t.error||`Error testing trunk`,`error`)}catch(e){console.error(`Error testing trunk:`,e),b(`Error testing trunk`,`error`)}}var Mr=[];function Nr(e){if(e.source===`manual`)return`Manual`;let t=e.shadowed?` <span class="badge" style="background:#6b7280;">shadowed by a manual route</span>`:``;return`via extension ${y(e.destination_value)}${t}`}async function Pr(){try{let e=await _(`${h()}/api/inbound-routes`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}: ${e.statusText}`);Mr=(await e.json()).routes??[];let t=document.getElementById(`inbound-routes-list`);if(!t)return;if(Mr.length===0){t.innerHTML=`<tr><td colspan="7" style="text-align: center;">No inbound routes configured</td></tr>`;return}t.innerHTML=Mr.map(e=>{let t=e.source===`manual`&&e.id!==void 0?`
                    <button class="btn-small btn-primary" onclick="editInboundRoute(${e.id})">Edit</button>
                    <button class="btn-small btn-danger" onclick="deleteInboundRoute(${e.id}, '${y(e.did_number)}')">Delete</button>
                `:`<small>Edit on Extensions page</small>`;return`
                <tr>
                    <td><strong>${y(e.did_number)}</strong></td>
                    <td>${e.trunk_id?y(e.trunk_id):`Any`}</td>
                    <td>${y(e.destination_type)}: ${y(e.destination_value)}</td>
                    <td>${e.priority}</td>
                    <td>${e.enabled?`Yes`:`No`}</td>
                    <td>${Nr(e)}</td>
                    <td>${t}</td>
                </tr>
            `}).join(``)}catch(e){console.error(`Error loading inbound routes:`,e),b(`Error loading inbound routes: ${e instanceof Error?e.message:String(e)}`,`error`)}}function Fr(){let e=document.getElementById(`inbound-route-form`);e&&e.reset();let t=document.getElementById(`inbound-route-id`);t&&(t.value=``);let n=document.getElementById(`inbound-route-modal-title`);n&&(n.textContent=`➕ Add Inbound Route`);let r=document.getElementById(`inbound-route-modal`);r&&(r.style.display=`block`)}function Ir(e){let t=Mr.find(t=>t.id===e);if(!t)return;let n=document.getElementById(`inbound-route-id`);n&&(n.value=String(e));let r=document.getElementById(`inbound-route-did`);r&&(r.value=t.did_number);let i=document.getElementById(`inbound-route-trunk`);i&&(i.value=t.trunk_id??``);let a=document.getElementById(`inbound-route-destination-type`);a&&(a.value=t.destination_type);let o=document.getElementById(`inbound-route-destination-value`);o&&(o.value=t.destination_value);let s=document.getElementById(`inbound-route-priority`);s&&(s.value=String(t.priority));let c=document.getElementById(`inbound-route-enabled`);c&&(c.checked=t.enabled);let l=document.getElementById(`inbound-route-modal-title`);l&&(l.textContent=`✏️ Edit Inbound Route (${t.did_number})`);let u=document.getElementById(`inbound-route-modal`);u&&(u.style.display=`block`)}function Lr(){let e=document.getElementById(`inbound-route-modal`);e&&(e.style.display=`none`);let t=document.getElementById(`inbound-route-form`);t&&t.reset()}async function Rr(e){e.preventDefault();let t=e=>document.getElementById(e)?.value??``,n=t(`inbound-route-id`),r={did_number:t(`inbound-route-did`),trunk_id:t(`inbound-route-trunk`),destination_type:t(`inbound-route-destination-type`),destination_value:t(`inbound-route-destination-value`),priority:parseInt(t(`inbound-route-priority`))||100,enabled:document.getElementById(`inbound-route-enabled`)?.checked??!0};try{let e=h(),t=await(await _(n?`${e}/api/inbound-routes/${n}`:`${e}/api/inbound-routes`,{method:n?`PUT`:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(r)})).json();t.success?(b(`Inbound route for ${r.did_number} saved`,`success`),Lr(),Pr()):b(t.error||`Error saving inbound route`,`error`)}catch(e){console.error(`Error saving inbound route:`,e),b(`Error saving inbound route`,`error`)}}async function zr(e,t){if(confirm(`Are you sure you want to delete the inbound route for "${t}"?`))try{let n=await(await _(`${h()}/api/inbound-routes/${e}`,{method:`DELETE`,headers:g()})).json();n.success?(b(`Inbound route for ${t} deleted`,`success`),Pr()):b(n.error||`Error deleting inbound route`,`error`)}catch(e){console.error(`Error deleting inbound route:`,e),b(`Error deleting inbound route`,`error`)}}async function Br(){try{let e=await _(`${h()}/api/lcr/rates`,{headers:g()});if(!e.ok){window.suppressErrorNotifications?debugLog(`LCR rates endpoint returned error:`,e.status,`(feature may not be enabled)`):(console.error(`Error loading LCR rates:`,e.status),b(`Error loading LCR rates`,`error`));return}let t=await e.json();if(t.rates!==void 0){let e=document.getElementById(`lcr-total-rates`);e&&(e.textContent=String(t.count||0));let n=document.getElementById(`lcr-time-rates`);n&&(n.textContent=String(t.time_rates?t.time_rates.length:0));let r=document.getElementById(`lcr-rates-list`);r&&(t.rates.length===0?r.innerHTML=`<tr><td colspan="7" style="text-align: center;">No rates configured</td></tr>`:r.innerHTML=t.rates.map(e=>`
                        <tr>
                            <td><strong>${y(e.trunk_id)}</strong></td>
                            <td><code>${y(e.pattern)}</code></td>
                            <td>${y(e.description)}</td>
                            <td>$${e.rate_per_minute.toFixed(4)}</td>
                            <td>$${e.connection_fee.toFixed(4)}</td>
                            <td>${e.minimum_seconds}s</td>
                            <td>${e.billing_increment}s</td>
                        </tr>
                    `).join(``));let i=document.getElementById(`lcr-time-rates-list`);i&&(!t.time_rates||t.time_rates.length===0?i.innerHTML=`<tr><td colspan="5" style="text-align: center;">No time-based rates configured</td></tr>`:i.innerHTML=t.time_rates.map(e=>{let t=[`Mon`,`Tue`,`Wed`,`Thu`,`Fri`,`Sat`,`Sun`],n=e.days_of_week.map(e=>t[e]).join(`, `);return`
                            <tr>
                                <td><strong>${y(e.name)}</strong></td>
                                <td>${e.start_time}</td>
                                <td>${e.end_time}</td>
                                <td>${n}</td>
                                <td>${e.rate_multiplier}x</td>
                            </tr>
                        `}).join(``))}Vr()}catch(e){if(window.suppressErrorNotifications){let t=e instanceof Error?e.message:String(e);debugLog(`Error loading LCR rates (expected if LCR not enabled):`,t)}else console.error(`Error loading LCR rates:`,e),b(`Error loading LCR rates`,`error`)}}async function Vr(){try{let e=await _(`${h()}/api/lcr/statistics`,{headers:g()});if(!e.ok){window.suppressErrorNotifications?debugLog(`LCR statistics endpoint returned error:`,e.status,`(feature may not be enabled)`):console.error(`Error loading LCR statistics:`,e.status);return}let t=await e.json(),n=document.getElementById(`lcr-total-routes`);n&&(n.textContent=String(t.total_routes||0));let r=document.getElementById(`lcr-status`);r&&(r.innerHTML=t.enabled?`<span class="badge" style="background: #10b981;">Enabled</span>`:`<span class="badge" style="background: #6b7280;">Disabled</span>`);let i=document.getElementById(`lcr-decisions-list`);i&&(!t.recent_decisions||t.recent_decisions.length===0?i.innerHTML=`<tr><td colspan="5" style="text-align: center;">No recent decisions</td></tr>`:i.innerHTML=t.recent_decisions.map(e=>`
                        <tr>
                            <td>${new Date(e.timestamp).toLocaleString()}</td>
                            <td>${y(e.number)}</td>
                            <td><strong>${y(e.selected_trunk)}</strong></td>
                            <td>$${e.estimated_cost.toFixed(4)}</td>
                            <td>${e.alternatives}</td>
                        </tr>
                    `).join(``))}catch(e){if(window.suppressErrorNotifications){let t=e instanceof Error?e.message:String(e);debugLog(`Error loading LCR statistics (expected if LCR not enabled):`,t)}else console.error(`Error loading LCR statistics:`,e)}}function Hr(){document.body.insertAdjacentHTML(`beforeend`,`
        <div id="lcr-rate-modal" class="modal" style="display: block;">
            <div class="modal-content" style="max-width: 600px;">
                <h2>Add LCR Rate</h2>
                <form id="add-lcr-rate-form" onsubmit="addLCRRate(event)">
                    <div class="form-group">
                        <label for="lcr-trunk-id">Trunk ID:</label>
                        <input type="text" id="lcr-trunk-id" required>
                        <small>The SIP trunk ID this rate applies to</small>
                    </div>

                    <div class="form-group">
                        <label for="lcr-pattern">Dial Pattern (Regex):</label>
                        <input type="text" id="lcr-pattern" required placeholder="^\\d{10}$">
                        <small>Regex pattern to match dialed numbers (e.g., ^\\d{10}$ for US local)</small>
                    </div>

                    <div class="form-group">
                        <label for="lcr-description">Description:</label>
                        <input type="text" id="lcr-description" placeholder="US Local Calls">
                    </div>

                    <div class="form-group">
                        <label for="lcr-rate-per-minute">Rate per Minute ($):</label>
                        <input type="number" id="lcr-rate-per-minute" step="0.0001" min="0" required placeholder="0.0100">
                    </div>

                    <div class="form-group">
                        <label for="lcr-connection-fee">Connection Fee ($):</label>
                        <input type="number" id="lcr-connection-fee" step="0.0001" min="0" value="0.0000">
                    </div>

                    <div class="form-group">
                        <label for="lcr-minimum-seconds">Minimum Billable Seconds:</label>
                        <input type="number" id="lcr-minimum-seconds" min="0" value="0">
                    </div>

                    <div class="form-group">
                        <label for="lcr-billing-increment">Billing Increment (seconds):</label>
                        <input type="number" id="lcr-billing-increment" min="1" value="1">
                        <small>Round up billing to this increment (e.g., 6 for 6-second increments)</small>
                    </div>

                    <div class="form-actions">
                        <button type="submit" class="btn btn-primary">Add Rate</button>
                        <button type="button" class="btn btn-secondary" onclick="closeLCRRateModal()">Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    `)}function Ur(){let e=document.getElementById(`lcr-rate-modal`);e&&e.remove()}function Wr(e){if(!e||e.trim().length===0)return{valid:!1,error:`Pattern cannot be empty`};try{return new RegExp(e),{valid:!0}}catch(e){return{valid:!1,error:`Invalid regex: ${e instanceof Error?e.message:`Invalid regex pattern`}`}}}async function Gr(e){e.preventDefault();let t=document.getElementById(`lcr-pattern`).value,n=Wr(t);if(!n.valid){b(n.error||`Invalid LCR pattern`,`error`);return}let r={trunk_id:document.getElementById(`lcr-trunk-id`).value,pattern:t,description:document.getElementById(`lcr-description`).value,rate_per_minute:parseFloat(document.getElementById(`lcr-rate-per-minute`).value),connection_fee:parseFloat(document.getElementById(`lcr-connection-fee`).value),minimum_seconds:parseInt(document.getElementById(`lcr-minimum-seconds`).value),billing_increment:parseInt(document.getElementById(`lcr-billing-increment`).value)};try{let e=await(await _(`${h()}/api/lcr/rate`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(r)})).json();e.success?(b(`LCR rate added successfully`,`success`),Ur(),Br()):b(e.error||`Error adding LCR rate`,`error`)}catch(e){console.error(`Error adding LCR rate:`,e),b(`Error adding LCR rate: ${e instanceof Error?e.message:`Unknown error`}`,`error`)}}function Kr(){document.body.insertAdjacentHTML(`beforeend`,`
        <div id="lcr-time-rate-modal" class="modal" style="display: block;">
            <div class="modal-content" style="max-width: 600px;">
                <h2>Add Time-Based Rate Modifier</h2>
                <form id="add-time-rate-form" onsubmit="addTimeRate(event)">
                    <div class="form-group">
                        <label for="time-rate-name">Period Name:</label>
                        <input type="text" id="time-rate-name" required placeholder="Peak Hours">
                    </div>

                    <div class="form-row">
                        <div class="form-group">
                            <label for="time-rate-start-hour">Start Hour (0-23):</label>
                            <input type="number" id="time-rate-start-hour" min="0" max="23" required value="9">
                        </div>
                        <div class="form-group">
                            <label for="time-rate-start-minute">Start Minute:</label>
                            <input type="number" id="time-rate-start-minute" min="0" max="59" required value="0">
                        </div>
                    </div>

                    <div class="form-row">
                        <div class="form-group">
                            <label for="time-rate-end-hour">End Hour (0-23):</label>
                            <input type="number" id="time-rate-end-hour" min="0" max="23" required value="17">
                        </div>
                        <div class="form-group">
                            <label for="time-rate-end-minute">End Minute:</label>
                            <input type="number" id="time-rate-end-minute" min="0" max="59" required value="0">
                        </div>
                    </div>

                    <div class="form-group">
                        <label>Days of Week:</label>
                        <div style="display: flex; gap: 10px; flex-wrap: wrap;">
                            <label><input type="checkbox" name="time-days" value="0" checked> Mon</label>
                            <label><input type="checkbox" name="time-days" value="1" checked> Tue</label>
                            <label><input type="checkbox" name="time-days" value="2" checked> Wed</label>
                            <label><input type="checkbox" name="time-days" value="3" checked> Thu</label>
                            <label><input type="checkbox" name="time-days" value="4" checked> Fri</label>
                            <label><input type="checkbox" name="time-days" value="5"> Sat</label>
                            <label><input type="checkbox" name="time-days" value="6"> Sun</label>
                        </div>
                    </div>

                    <div class="form-group">
                        <label for="time-rate-multiplier">Rate Multiplier:</label>
                        <input type="number" id="time-rate-multiplier" step="0.1" min="0.1" required value="1.0">
                        <small>Multiply rates by this factor during this period (e.g., 1.2 for 20% increase)</small>
                    </div>

                    <div class="form-actions">
                        <button type="submit" class="btn btn-primary">Add Time Rate</button>
                        <button type="button" class="btn btn-secondary" onclick="closeTimeRateModal()">Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    `)}function qr(){let e=document.getElementById(`lcr-time-rate-modal`);e&&e.remove()}async function Jr(e){e.preventDefault();let t=Array.from(document.querySelectorAll(`input[name="time-days"]:checked`)).map(e=>parseInt(e.value)),n={name:document.getElementById(`time-rate-name`).value,start_hour:parseInt(document.getElementById(`time-rate-start-hour`).value),start_minute:parseInt(document.getElementById(`time-rate-start-minute`).value),end_hour:parseInt(document.getElementById(`time-rate-end-hour`).value),end_minute:parseInt(document.getElementById(`time-rate-end-minute`).value),days:t,multiplier:parseFloat(document.getElementById(`time-rate-multiplier`).value)};try{let e=await(await _(`${h()}/api/lcr/time-rate`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(n)})).json();e.success?(b(`Time-based rate added successfully`,`success`),qr(),Br()):b(e.error||`Error adding time-based rate`,`error`)}catch(e){console.error(`Error adding time-based rate:`,e),b(`Error adding time-based rate`,`error`)}}async function Yr(){if(confirm(`Are you sure you want to clear all LCR rates? This cannot be undone.`))try{let e=await(await _(`${h()}/api/lcr/clear-rates`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify({})})).json();e.success?(b(`All LCR rates cleared`,`success`),Br()):b(e.error||`Error clearing LCR rates`,`error`)}catch(e){console.error(`Error clearing LCR rates:`,e),b(`Error clearing LCR rates`,`error`)}}window.loadSIPTrunks=A,window.loadTrunkHealth=Er,window.showAddTrunkModal=Dr,window.closeAddTrunkModal=Or,window.addSIPTrunk=kr,window.deleteTrunk=Ar,window.testTrunk=jr,window.loadInboundRoutes=Pr,window.showAddInboundRouteModal=Fr,window.editInboundRoute=Ir,window.closeInboundRouteModal=Lr,window.saveInboundRoute=Rr,window.deleteInboundRoute=zr,window.loadLCRRates=Br,window.loadLCRStatistics=Vr,window.showAddLCRRateModal=Hr,window.closeLCRRateModal=Ur,window.addLCRRate=Gr,window.showAddTimeRateModal=Kr,window.closeTimeRateModal=qr,window.addTimeRate=Jr,window.clearLCRRates=Yr;var Xr=0;async function Zr(){try{let e=await _(`${h()}/api/fmfm/extensions`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}: ${e.statusText}`);let t=await e.json();if(t.extensions){let e=document.getElementById(`fmfm-total-extensions`);e&&(e.textContent=String(t.count||0));let n=t.extensions.filter(e=>e.mode===`sequential`).length,r=t.extensions.filter(e=>e.mode===`simultaneous`).length,i=t.extensions.filter(e=>e.enabled!==!1).length,a=document.getElementById(`fmfm-sequential`);a&&(a.textContent=String(n));let o=document.getElementById(`fmfm-simultaneous`);o&&(o.textContent=String(r));let s=document.getElementById(`fmfm-active-count`);s&&(s.textContent=String(i));let c=document.getElementById(`fmfm-list`);if(!c)return;t.extensions.length===0?c.innerHTML=`<tr><td colspan="6" style="text-align: center;">No Find Me/Follow Me configurations</td></tr>`:c.innerHTML=t.extensions.map(e=>{let t=e.enabled!==!1,n=e.mode===`sequential`?`<span class="badge" style="background: #3b82f6;">Sequential</span>`:`<span class="badge" style="background: #10b981;">Simultaneous</span>`,r=t?`<span class="badge" style="background: #10b981;">Active</span>`:`<span class="badge" style="background: #6b7280;">Disabled</span>`,i=e.destinations||[],a=i.map(e=>`${y(e.number)}${e.ring_time?` (${e.ring_time}s)`:``}`).join(`, `),o=e.updated_at?new Date(e.updated_at).toLocaleString():`N/A`;return`
                        <tr>
                            <td><strong>${y(e.extension)}</strong></td>
                            <td>${n}</td>
                            <td>
                                <div style="max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${y(a)}">
                                    ${i.length} destination(s): ${y(a)||`None`}
                                </div>
                            </td>
                            <td>${r}</td>
                            <td><small>${o}</small></td>
                            <td>
                                <button class="btn-small btn-primary" data-config='${y(JSON.stringify(e))}' onclick="editFMFMConfig(JSON.parse(this.getAttribute('data-config')))">Edit</button>
                                <button class="btn-small btn-danger" onclick="deleteFMFMConfig('${y(e.extension)}')">Delete</button>
                            </td>
                        </tr>
                    `}).join(``)}}catch(e){console.error(`Error loading FMFM extensions:`,e),b(`Error loading FMFM configurations`,`error`)}}function Qr(){let e=document.getElementById(`add-fmfm-modal`);e&&(e.style.display=`block`);let t=document.getElementById(`fmfm-extension`);t&&(t.value=``,t.readOnly=!1);let n=document.getElementById(`fmfm-mode`);n&&(n.value=`sequential`);let r=document.getElementById(`fmfm-enabled`);r&&(r.checked=!0);let i=document.getElementById(`fmfm-no-answer`);i&&(i.value=``);let a=document.getElementById(`fmfm-destinations-list`);a&&(a.innerHTML=``),ei()}function $r(){let e=document.getElementById(`add-fmfm-modal`);e&&(e.style.display=`none`);let t=document.getElementById(`add-fmfm-form`);t&&t.reset()}function ei(){let e=document.getElementById(`fmfm-destinations-list`);if(!e)return;let t=`fmfm-dest-${Xr++}`,n=document.createElement(`div`);n.id=t,n.style.cssText=`display: flex; gap: 10px; margin-bottom: 10px; align-items: center;`,n.innerHTML=`
        <input type="text" class="fmfm-dest-number" placeholder="Phone number or extension" required style="flex: 2;">
        <input type="number" class="fmfm-dest-ringtime" placeholder="Ring time (s)" value="20" min="5" max="120" style="flex: 1;">
        <button type="button" class="btn-small btn-danger" onclick="document.getElementById('${t}').remove()">Remove</button>
    `,e.appendChild(n)}async function ti(e){e.preventDefault();let t=document.getElementById(`fmfm-extension`).value,n=document.getElementById(`fmfm-mode`).value,r=document.getElementById(`fmfm-enabled`).checked,i=document.getElementById(`fmfm-no-answer`).value,a=Array.from(document.querySelectorAll(`.fmfm-dest-number`)),o=Array.from(document.querySelectorAll(`.fmfm-dest-ringtime`)),s=a.map((e,t)=>({number:e.value,ring_time:parseInt(o[t]?.value??`20`)||20})).filter(e=>e.number);if(s.length===0){b(`At least one destination is required`,`error`);return}let c={extension:t,mode:n,enabled:r,destinations:s};i&&(c.no_answer_destination=i);try{let e=await(await _(`${h()}/api/fmfm/config`,{method:`POST`,headers:g(),body:JSON.stringify(c)})).json();e.success?(b(`FMFM configured for extension ${t}`,`success`),$r(),Zr()):b(e.error||`Error configuring FMFM`,`error`)}catch(e){console.error(`Error saving FMFM config:`,e),b(`Error saving FMFM configuration`,`error`)}}function ni(e){Qr();let t=document.getElementById(`fmfm-extension`);t&&(t.value=e.extension,t.readOnly=!0);let n=document.getElementById(`fmfm-mode`);n&&(n.value=e.mode);let r=document.getElementById(`fmfm-enabled`);r&&(r.checked=e.enabled!==!1);let i=document.getElementById(`fmfm-no-answer`);i&&(i.value=e.no_answer_destination||``);let a=document.getElementById(`fmfm-destinations-list`);if(a)if(a.innerHTML=``,e.destinations&&e.destinations.length>0)for(let t of e.destinations){ei();let e=a.children,n=e[e.length-1],r=n.querySelector(`.fmfm-dest-number`);r&&(r.value=t.number);let i=n.querySelector(`.fmfm-dest-ringtime`);i&&(i.value=String(t.ring_time??20))}else ei()}async function ri(e){if(confirm(`Are you sure you want to delete FMFM configuration for extension ${e}?`))try{let t=await(await _(`${h()}/api/fmfm/config/${e}`,{method:`DELETE`,headers:g()})).json();t.success?(b(`FMFM configuration deleted for ${e}`,`success`),Zr()):b(t.error||`Error deleting FMFM configuration`,`error`)}catch(e){console.error(`Error deleting FMFM config:`,e),b(`Error deleting FMFM configuration`,`error`)}}function ii(e){let t=[];if(e.days_of_week){let n=[`Mon`,`Tue`,`Wed`,`Thu`,`Fri`,`Sat`,`Sun`],r=e.days_of_week.map(e=>n[e]).join(`, `);t.push(r)}return e.start_time&&e.end_time&&t.push(`${e.start_time}-${e.end_time}`),e.holidays===!0?t.push(`Holidays`):e.holidays===!1&&t.push(`Non-holidays`),t.length>0?t.join(` | `):`Always`}async function ai(){try{let e=await _(`${h()}/api/time-routing/rules`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}: ${e.statusText}`);let t=await e.json();if(t.rules){let e=document.getElementById(`time-routing-total`);e&&(e.textContent=String(t.count||0));let n=t.rules.filter(e=>e.enabled!==!1).length,r=t.rules.filter(e=>e.name&&(e.name.toLowerCase().includes(`business`)||e.name.toLowerCase().includes(`hours`))).length,i=t.rules.filter(e=>e.name&&(e.name.toLowerCase().includes(`after`)||e.name.toLowerCase().includes(`closed`))).length,a=document.getElementById(`time-routing-active`);a&&(a.textContent=String(n));let o=document.getElementById(`time-routing-business`);o&&(o.textContent=String(r));let s=document.getElementById(`time-routing-after`);s&&(s.textContent=String(i));let c=document.getElementById(`time-routing-list`);if(!c)return;t.rules.length===0?c.innerHTML=`<tr><td colspan="7" style="text-align: center;">No time-based routing rules</td></tr>`:c.innerHTML=t.rules.map(e=>{let t=e.enabled===!1?`<span class="badge" style="background: #6b7280;">Disabled</span>`:`<span class="badge" style="background: #10b981;">Active</span>`,n=ii(e.time_conditions||{});return`
                        <tr>
                            <td><strong>${y(e.name)}</strong></td>
                            <td>${y(e.destination)}</td>
                            <td>${y(e.route_to)}</td>
                            <td><small>${y(n)}</small></td>
                            <td>${e.priority||100}</td>
                            <td>${t}</td>
                            <td>
                                <button class="btn-small btn-danger" onclick="deleteTimeRoutingRule('${y(e.rule_id)}', '${y(e.name)}')">Delete</button>
                            </td>
                        </tr>
                    `}).join(``)}}catch(e){console.error(`Error loading time routing rules:`,e),b(`Error loading time routing rules`,`error`)}}function oi(){let e=document.getElementById(`add-time-rule-modal`);e&&(e.style.display=`block`)}function si(){let e=document.getElementById(`add-time-rule-modal`);e&&(e.style.display=`none`);let t=document.getElementById(`add-time-rule-form`);t&&t.reset()}async function ci(e){e.preventDefault();let t=document.getElementById(`time-rule-name`).value,n=document.getElementById(`time-rule-destination`).value,r=document.getElementById(`time-rule-route-to`).value,i=document.getElementById(`time-rule-start`).value,a=document.getElementById(`time-rule-end`).value,o=parseInt(document.getElementById(`time-rule-priority`).value),s=document.getElementById(`time-rule-enabled`).checked,c=Array.from(document.querySelectorAll(`input[name="time-rule-days"]:checked`)).map(e=>parseInt(e.value));if(c.length===0){b(`Please select at least one day of the week`,`error`);return}let l={name:t,destination:n,route_to:r,priority:o,enabled:s,time_conditions:{days_of_week:c,start_time:i,end_time:a}};try{let e=await(await _(`${h()}/api/time-routing/rule`,{method:`POST`,headers:g(),body:JSON.stringify(l)})).json();e.success?(b(`Time routing rule "${t}" added successfully`,`success`),si(),ai()):b(e.error||`Error adding time routing rule`,`error`)}catch(e){console.error(`Error saving time routing rule:`,e),b(`Error saving time routing rule`,`error`)}}async function li(e,t){if(confirm(`Are you sure you want to delete time routing rule "${t}"?`))try{let n=await(await _(`${h()}/api/time-routing/rule/${e}`,{method:`DELETE`,headers:g()})).json();n.success?(b(`Time routing rule "${t}" deleted`,`success`),ai()):b(n.error||`Error deleting time routing rule`,`error`)}catch(e){console.error(`Error deleting time routing rule:`,e),b(`Error deleting time routing rule`,`error`)}}async function ui(){try{let e=await _(`${h()}/api/webhooks`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}: ${e.statusText}`);let t=await e.json();if(t.subscriptions){let e=document.getElementById(`webhooks-list`);if(!e)return;t.subscriptions.length===0?e.innerHTML=`<tr><td colspan="5" style="text-align: center;">No webhooks configured</td></tr>`:e.innerHTML=t.subscriptions.map(e=>{let t=e.enabled===!1?`<span class="badge" style="background: #6b7280;">Disabled</span>`:`<span class="badge" style="background: #10b981;">Active</span>`,n=(e.event_types||[]).join(`, `),r=e.secret?`Yes`:`No`;return`
                        <tr>
                            <td>
                                <div style="max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${y(e.url)}">
                                    ${y(e.url)}
                                </div>
                            </td>
                            <td>
                                <div style="max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;" title="${y(n)}">
                                    <small>${y(n)}</small>
                                </div>
                            </td>
                            <td>${r}</td>
                            <td>${t}</td>
                            <td>
                                <button class="btn-small btn-danger" onclick="deleteWebhook('${y(e.url)}')">Delete</button>
                            </td>
                        </tr>
                    `}).join(``)}}catch(e){console.error(`Error loading webhooks:`,e),b(`Error loading webhooks`,`error`)}}function di(){let e=document.getElementById(`add-webhook-modal`);e&&(e.style.display=`block`)}function fi(){let e=document.getElementById(`add-webhook-modal`);e&&(e.style.display=`none`);let t=document.getElementById(`add-webhook-form`);t&&t.reset()}async function pi(e){e.preventDefault();let t=document.getElementById(`webhook-url`).value,n=document.getElementById(`webhook-secret`).value,r=document.getElementById(`webhook-enabled`).checked,i=Array.from(document.querySelectorAll(`input[name="webhook-events"]:checked`)).map(e=>e.value);if(i.length===0){b(`Please select at least one event type`,`error`);return}let a={url:t,event_types:i,enabled:r};n&&(a.secret=n);try{let e=await(await _(`${h()}/api/webhooks`,{method:`POST`,headers:g(),body:JSON.stringify(a)})).json();e.success?(b(`Webhook added successfully`,`success`),fi(),ui()):b(e.error||`Error adding webhook`,`error`)}catch(e){console.error(`Error adding webhook:`,e),b(`Error adding webhook`,`error`)}}async function mi(e){if(!confirm(`Are you sure you want to delete webhook for ${e}?`))return;let t=encodeURIComponent(e);try{let e=await(await _(`${h()}/api/webhooks/${t}`,{method:`DELETE`,headers:g()})).json();e.success?(b(`Webhook deleted`,`success`),ui()):b(e.error||`Error deleting webhook`,`error`)}catch(e){console.error(`Error deleting webhook:`,e),b(`Error deleting webhook`,`error`)}}function hi(e){let t=new Date().getTime()-e.getTime(),n=Math.floor(t/(1e3*60*60)),r=Math.floor(t%(1e3*60*60)/(1e3*60));return n>0?`${n}h ${r}m`:`${r}m`}async function gi(){try{let e=await _(`${h()}/api/hot-desk/sessions`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}: ${e.statusText}`);let t=await e.json();if(t.sessions){let e=t.sessions.filter(e=>e.active!==!1),n=document.getElementById(`hotdesk-active`);n&&(n.textContent=String(e.length));let r=document.getElementById(`hotdesk-total`);r&&(r.textContent=String(t.sessions.length));let i=document.getElementById(`hotdesk-sessions-list`);if(!i)return;e.length===0?i.innerHTML=`<tr><td colspan="6" style="text-align: center;">No active hot desk sessions</td></tr>`:i.innerHTML=e.map(e=>{let t=e.login_time?new Date(e.login_time).toLocaleString():`N/A`,n=e.login_time?hi(new Date(e.login_time)):`N/A`;return`
                        <tr>
                            <td><strong>${y(e.extension)}</strong></td>
                            <td>${y(e.device_mac||`N/A`)}</td>
                            <td>${y(e.device_ip||`N/A`)}</td>
                            <td><small>${t}</small></td>
                            <td>${n}</td>
                            <td>
                                <button class="btn-small btn-warning" onclick="logoutHotDesk('${y(e.extension)}')">Logout</button>
                            </td>
                        </tr>
                    `}).join(``)}}catch(e){console.error(`Error loading hot desk sessions:`,e),b(`Error loading hot desk sessions`,`error`)}}async function _i(e){if(confirm(`Are you sure you want to log out extension ${e} from hot desk?`))try{let t=await(await _(`${h()}/api/hot-desk/logout`,{method:`POST`,headers:g(),body:JSON.stringify({extension:e})})).json();t.success?(b(`Extension ${e} logged out`,`success`),gi()):b(t.error||`Error logging out`,`error`)}catch(e){console.error(`Error logging out hot desk:`,e),b(`Error logging out hot desk`,`error`)}}async function vi(){try{let e=h(),[t,n]=await Promise.all([_(`${e}/api/recording-retention/policies`,{headers:g()}),_(`${e}/api/recording-retention/statistics`,{headers:g()})]),[r,i]=await Promise.all([t.json(),n.json()]);if(i){let e=document.getElementById(`retention-policies-count`);e&&(e.textContent=String(i.total_policies||0));let t=document.getElementById(`retention-recordings`);t&&(t.textContent=String(i.total_recordings||0));let n=document.getElementById(`retention-deleted`);n&&(n.textContent=String(i.deleted_count||0));let r=i.last_cleanup?new Date(i.last_cleanup).toLocaleDateString():`Never`,a=document.getElementById(`retention-last-cleanup`);a&&(a.textContent=r)}if(r&&r.policies){let e=document.getElementById(`retention-policies-list`);if(!e)return;r.policies.length===0?e.innerHTML=`<tr><td colspan="5" style="text-align: center;">No retention policies configured</td></tr>`:e.innerHTML=r.policies.map(e=>{let t=e.created_at?new Date(e.created_at).toLocaleDateString():`N/A`,n=e.tags?e.tags.join(`, `):`None`;return`
                        <tr>
                            <td><strong>${y(e.name)}</strong></td>
                            <td>${e.retention_days} days</td>
                            <td><small>${y(n)}</small></td>
                            <td><small>${t}</small></td>
                            <td>
                                <button class="btn-small btn-danger" onclick="deleteRetentionPolicy('${y(e.policy_id)}', '${y(e.name)}')">Delete</button>
                            </td>
                        </tr>
                    `}).join(``)}}catch(e){console.error(`Error loading retention policies:`,e),b(`Error loading retention policies`,`error`)}}function yi(){let e=document.getElementById(`add-retention-policy-modal`);e&&(e.style.display=`block`)}function bi(){let e=document.getElementById(`add-retention-policy-modal`);e&&(e.style.display=`none`);let t=document.getElementById(`add-retention-policy-form`);t&&t.reset()}async function xi(e){e.preventDefault();let t=document.getElementById(`retention-policy-name`).value,n=parseInt(document.getElementById(`retention-days`).value),r=document.getElementById(`retention-tags`).value;if(!t.match(/^[a-zA-Z0-9_\s-]+$/)){b(`Policy name contains invalid characters`,`error`);return}if(n<1||n>3650){b(`Retention days must be between 1 and 3650`,`error`);return}let i={name:t,retention_days:n};r.trim()&&(i.tags=r.split(`,`).map(e=>e.trim()).filter(e=>e));try{let e=await(await _(`${h()}/api/recording-retention/policy`,{method:`POST`,headers:g(),body:JSON.stringify(i)})).json();e.success?(b(`Retention policy "${t}" added successfully`,`success`),bi(),vi()):b(e.error||`Error adding retention policy`,`error`)}catch(e){console.error(`Error adding retention policy:`,e),b(`Error adding retention policy`,`error`)}}async function Si(e,t){if(confirm(`Are you sure you want to delete retention policy "${t}"?`))try{let n=await(await _(`${h()}/api/recording-retention/policy/${encodeURIComponent(e)}`,{method:`DELETE`,headers:g()})).json();n.success?(b(`Retention policy "${t}" deleted`,`success`),vi()):b(n.error||`Error deleting retention policy`,`error`)}catch(e){console.error(`Error deleting retention policy:`,e),b(`Error deleting retention policy`,`error`)}}async function j(){try{let e=h(),[t,n]=await Promise.all([_(`${e}/api/callback-queue/list`,{headers:g()}),_(`${e}/api/callback-queue/statistics`,{headers:g()})]),[r,i]=await Promise.all([t.json(),n.json()]);if(i){let e=document.getElementById(`callback-total`);e&&(e.textContent=String(i.total_callbacks||0));let t=i.status_breakdown||{},n=document.getElementById(`callback-scheduled`);n&&(n.textContent=String(t.scheduled||0));let r=document.getElementById(`callback-in-progress`);r&&(r.textContent=String(t.in_progress||0));let a=document.getElementById(`callback-completed`);a&&(a.textContent=String(t.completed||0));let o=document.getElementById(`callback-failed`);o&&(o.textContent=String(t.failed||0))}if(r&&r.callbacks){let e=document.getElementById(`callback-list`);if(!e)return;r.callbacks.length===0?e.innerHTML=`<tr><td colspan="8" style="text-align: center;">No callbacks in queue</td></tr>`:e.innerHTML=r.callbacks.map(e=>{let t=new Date(e.requested_at).toLocaleString(),n=new Date(e.callback_time).toLocaleString(),r=``;switch(e.status){case`scheduled`:r=`badge-info`;break;case`in_progress`:r=`badge-warning`;break;case`completed`:r=`badge-success`;break;case`failed`:r=`badge-danger`;break;case`cancelled`:r=`badge-secondary`;break;default:r=`badge-info`}return`
                        <tr>
                            <td><code>${y(e.callback_id)}</code></td>
                            <td>${y(e.queue_id)}</td>
                            <td>
                                <strong>${y(e.caller_number)}</strong><br>
                                <small>${y(e.caller_name||`N/A`)}</small>
                            </td>
                            <td><small>${t}</small></td>
                            <td><small>${n}</small></td>
                            <td><span class="badge ${r}">${y(e.status)}</span></td>
                            <td>${e.attempts}</td>
                            <td>
                                ${e.status===`scheduled`?`
                                    <button class="btn-small btn-primary" onclick="startCallback('${y(e.callback_id)}')">Start</button>
                                    <button class="btn-small btn-danger" onclick="cancelCallback('${y(e.callback_id)}')">Cancel</button>
                                `:e.status===`in_progress`?`
                                    <button class="btn-small btn-success" onclick="completeCallback('${y(e.callback_id)}', true)">Done</button>
                                    <button class="btn-small btn-warning" onclick="completeCallback('${y(e.callback_id)}', false)">Retry</button>
                                `:`-`}
                            </td>
                        </tr>
                    `}).join(``)}}catch(e){console.error(`Error loading callback queue:`,e),b(`Error loading callback queue`,`error`)}}function Ci(){let e=document.createElement(`div`);e.className=`modal`,e.id=`request-callback-modal`,e.innerHTML=`
        <div class="modal-content">
            <span class="close" onclick="closeRequestCallbackModal()">&times;</span>
            <h2>Request Callback</h2>
            <form id="request-callback-form" onsubmit="requestCallback(event)">
                <div class="form-group">
                    <label for="callback-queue-id">Queue ID: *</label>
                    <input type="text" id="callback-queue-id" required
                           placeholder="e.g., sales, support, general">
                </div>
                <div class="form-group">
                    <label for="callback-caller-number">Caller Number: *</label>
                    <input type="tel" id="callback-caller-number" required
                           placeholder="e.g., +1234567890">
                </div>
                <div class="form-group">
                    <label for="callback-caller-name">Caller Name:</label>
                    <input type="text" id="callback-caller-name"
                           placeholder="Optional">
                </div>
                <div class="form-group">
                    <label for="callback-preferred-time">Preferred Time:</label>
                    <input type="datetime-local" id="callback-preferred-time">
                    <small>Leave empty for ASAP callback</small>
                </div>
                <div class="form-actions">
                    <button type="button" class="btn btn-secondary" onclick="closeRequestCallbackModal()">Cancel</button>
                    <button type="submit" class="btn btn-success">Request Callback</button>
                </div>
            </form>
        </div>
    `,document.body.appendChild(e),e.style.display=`block`}function wi(){let e=document.getElementById(`request-callback-modal`);e&&e.remove()}async function Ti(e){e.preventDefault();let t=document.getElementById(`callback-queue-id`).value,n=document.getElementById(`callback-caller-number`).value,r=document.getElementById(`callback-caller-name`).value,i=document.getElementById(`callback-preferred-time`).value,a={queue_id:t,caller_number:n};r&&(a.caller_name=r),i&&(a.preferred_time=new Date(i).toISOString());try{let e=await(await _(`${h()}/api/callback-queue/request`,{method:`POST`,headers:g(),body:JSON.stringify(a)})).json();e.success?(b(`Callback requested successfully`,`success`),wi(),j()):b(e.error||`Error requesting callback`,`error`)}catch(e){console.error(`Error requesting callback:`,e),b(`Error requesting callback`,`error`)}}async function Ei(e){let t=prompt(`Enter your agent ID/extension:`);if(t)try{let n=await(await _(`${h()}/api/callback-queue/start`,{method:`POST`,headers:g(),body:JSON.stringify({callback_id:e,agent_id:t})})).json();n.success?(b(`Started callback to ${n.caller_number??`caller`}`,`success`),j()):b(n.error||`Error starting callback`,`error`)}catch(e){console.error(`Error starting callback:`,e),b(`Error starting callback`,`error`)}}async function Di(e,t){let n=``;t||(n=prompt(`Enter reason for failure (optional):`)||``);try{let r=await(await _(`${h()}/api/callback-queue/complete`,{method:`POST`,headers:g(),body:JSON.stringify({callback_id:e,success:t,notes:n})})).json();r.success?(b(t?`Callback completed`:`Callback will be retried`,`success`),j()):b(r.error||`Error completing callback`,`error`)}catch(e){console.error(`Error completing callback:`,e),b(`Error completing callback`,`error`)}}async function Oi(e){if(confirm(`Are you sure you want to cancel this callback request?`))try{let t=await(await _(`${h()}/api/callback-queue/cancel`,{method:`POST`,headers:g(),body:JSON.stringify({callback_id:e})})).json();t.success?(b(`Callback cancelled`,`success`),j()):b(t.error||`Error cancelling callback`,`error`)}catch(e){console.error(`Error cancelling callback:`,e),b(`Error cancelling callback`,`error`)}}window.loadFMFMExtensions=Zr,window.showAddFMFMModal=Qr,window.closeAddFMFMModal=$r,window.addFMFMDestinationRow=ei,window.saveFMFMConfig=ti,window.editFMFMConfig=ni,window.deleteFMFMConfig=ri,window.getScheduleDescription=ii,window.showAddTimeRuleModal=oi,window.closeAddTimeRuleModal=si,window.loadTimeRoutingRules=ai,window.saveTimeRoutingRule=ci,window.deleteTimeRoutingRule=li,window.showAddWebhookModal=di,window.closeAddWebhookModal=fi,window.loadWebhooks=ui,window.addWebhook=pi,window.deleteWebhook=mi,window.loadHotDeskSessions=gi,window.logoutHotDesk=_i,window.getDuration=hi,window.loadRetentionPolicies=vi,window.showAddRetentionPolicyModal=yi,window.closeAddRetentionPolicyModal=bi,window.addRetentionPolicy=xi,window.deleteRetentionPolicy=Si,window.loadCallbackQueue=j,window.showRequestCallbackModal=Ci,window.closeRequestCallbackModal=wi,window.requestCallback=Ti,window.startCallback=Ei,window.completeCallback=Di,window.cancelCallback=Oi;async function ki(){try{let e=h(),[t,n]=await Promise.all([_(`${e}/api/fraud-detection/alerts?hours=24`,{headers:g()}),_(`${e}/api/fraud-detection/statistics`,{headers:g()})]),[r,i]=await Promise.all([t.json(),n.json()]);if(i){let e=e=>document.getElementById(e);e(`fraud-total-alerts`)&&(e(`fraud-total-alerts`).textContent=String(i.total_alerts??0)),e(`fraud-high-risk`)&&(e(`fraud-high-risk`).textContent=String(i.high_risk_alerts??0)),e(`fraud-blocked-patterns`)&&(e(`fraud-blocked-patterns`).textContent=String(i.blocked_patterns_count??0)),e(`fraud-extensions-flagged`)&&(e(`fraud-extensions-flagged`).textContent=String(i.extensions_flagged??0))}if(r?.alerts){let e=document.getElementById(`fraud-alerts-list`);e&&(r.alerts.length===0?e.innerHTML=`<tr><td colspan="5" style="text-align: center;">No fraud alerts detected</td></tr>`:e.innerHTML=r.alerts.map(e=>{let t=new Date(e.timestamp).toLocaleString(),n=e.fraud_score>.8?`#ef4444`:e.fraud_score>.5?`#f59e0b`:`#10b981`,r=(e.fraud_score*100).toFixed(0),i=(e.alert_types??[]).join(`, `);return`
                            <tr>
                                <td><small>${y(t)}</small></td>
                                <td><strong>${y(e.extension)}</strong></td>
                                <td><small>${y(i)}</small></td>
                                <td>
                                    <div style="display: flex; align-items: center; gap: 5px;">
                                        <div style="flex: 1; background: #e5e7eb; border-radius: 4px; height: 20px; overflow: hidden;">
                                            <div style="background: ${n}; height: 100%; width: ${r}%;"></div>
                                        </div>
                                        <span>${r}%</span>
                                    </div>
                                </td>
                                <td><small>${y(e.details??`No details`)}</small></td>
                            </tr>
                        `}).join(``))}if(i?.blocked_patterns){let e=document.getElementById(`blocked-patterns-list`);e&&(i.blocked_patterns.length===0?e.innerHTML=`<tr><td colspan="3" style="text-align: center;">No blocked patterns</td></tr>`:e.innerHTML=i.blocked_patterns.map((e,t)=>`
                        <tr>
                            <td><code>${y(e.pattern)}</code></td>
                            <td>${y(e.reason)}</td>
                            <td>
                                <button class="btn-small btn-danger" onclick="deleteBlockedPattern(${t}, '${y(e.pattern)}')">Delete</button>
                            </td>
                        </tr>
                    `).join(``))}}catch(e){console.error(`Error loading fraud detection data:`,e),b(`Error loading fraud detection data`,`error`)}}function Ai(){let e=document.getElementById(`add-blocked-pattern-modal`);e&&(e.style.display=`block`)}function ji(){let e=document.getElementById(`add-blocked-pattern-modal`);e&&(e.style.display=`none`);let t=document.getElementById(`add-blocked-pattern-form`);t&&t.reset()}async function Mi(e){e.preventDefault();let t=document.getElementById(`blocked-pattern`),n=document.getElementById(`blocked-reason`),r=t?.value??``,i=n?.value??``;try{new RegExp(r)}catch(e){b(`Invalid regex pattern: ${e instanceof Error?e.message:String(e)}`,`error`);return}let a={pattern:r,reason:i};try{let e=await(await _(`${h()}/api/fraud-detection/blocked-pattern`,{method:`POST`,headers:g(),body:JSON.stringify(a)})).json();e.success?(b(`Blocked pattern added successfully`,`success`),ji(),ki()):b(e.error??`Error adding blocked pattern`,`error`)}catch(e){console.error(`Error adding blocked pattern:`,e),b(`Error adding blocked pattern`,`error`)}}async function Ni(e,t){if(confirm(`Are you sure you want to unblock pattern "${t}"?`))try{let t=await(await _(`${h()}/api/fraud-detection/blocked-pattern/${e}`,{method:`DELETE`,headers:g()})).json();t.success?(b(`Blocked pattern removed`,`success`),ki()):b(t.error??`Error removing blocked pattern`,`error`)}catch(e){console.error(`Error removing blocked pattern:`,e),b(`Error removing blocked pattern`,`error`)}}async function M(){try{let e=h(),[t,n]=await Promise.all([_(`${e}/api/callback-queue/list`,{headers:g()}),_(`${e}/api/callback-queue/statistics`,{headers:g()})]),[r,i]=await Promise.all([t.json(),n.json()]);if(i){let e=e=>document.getElementById(e);e(`callback-total`)&&(e(`callback-total`).textContent=String(i.total_callbacks??0));let t=i.status_breakdown??{};e(`callback-scheduled`)&&(e(`callback-scheduled`).textContent=String(t.scheduled??0)),e(`callback-in-progress`)&&(e(`callback-in-progress`).textContent=String(t.in_progress??0)),e(`callback-completed`)&&(e(`callback-completed`).textContent=String(t.completed??0)),e(`callback-failed`)&&(e(`callback-failed`).textContent=String(t.failed??0))}if(r?.callbacks){let e=document.getElementById(`callback-list`);e&&(r.callbacks.length===0?e.innerHTML=`<tr><td colspan="8" style="text-align: center;">No callbacks in queue</td></tr>`:e.innerHTML=r.callbacks.map(e=>{let t=new Date(e.requested_at).toLocaleString(),n=new Date(e.callback_time).toLocaleString(),r=``;switch(e.status){case`scheduled`:r=`badge-info`;break;case`in_progress`:r=`badge-warning`;break;case`completed`:r=`badge-success`;break;case`failed`:r=`badge-danger`;break;case`cancelled`:r=`badge-secondary`;break;default:r=`badge-info`}return`
                            <tr>
                                <td><code>${y(e.callback_id)}</code></td>
                                <td>${y(e.queue_id)}</td>
                                <td>
                                    <strong>${y(e.caller_number)}</strong><br>
                                    <small>${y(e.caller_name??`N/A`)}</small>
                                </td>
                                <td><small>${y(t)}</small></td>
                                <td><small>${y(n)}</small></td>
                                <td><span class="badge ${r}">${y(e.status)}</span></td>
                                <td>${e.attempts}</td>
                                <td>
                                    ${e.status===`scheduled`?`
                                        <button class="btn-small btn-primary" onclick="startCallback('${y(e.callback_id)}')">Start</button>
                                        <button class="btn-small btn-danger" onclick="cancelCallback('${y(e.callback_id)}')">Cancel</button>
                                    `:e.status===`in_progress`?`
                                        <button class="btn-small btn-success" onclick="completeCallback('${y(e.callback_id)}', true)">Done</button>
                                        <button class="btn-small btn-warning" onclick="completeCallback('${y(e.callback_id)}', false)">Retry</button>
                                    `:`-`}
                                </td>
                            </tr>
                        `}).join(``))}}catch(e){console.error(`Error loading callback queue:`,e),b(`Error loading callback queue`,`error`)}}function Pi(){let e=document.createElement(`div`);e.className=`modal`,e.id=`request-callback-modal`,e.innerHTML=`
        <div class="modal-content">
            <span class="close" onclick="closeRequestCallbackModal()">&times;</span>
            <h2>Request Callback</h2>
            <form id="request-callback-form" onsubmit="requestCallback(event)">
                <div class="form-group">
                    <label for="callback-queue-id">Queue ID: *</label>
                    <input type="text" id="callback-queue-id" required
                           placeholder="e.g., sales, support, general">
                </div>
                <div class="form-group">
                    <label for="callback-caller-number">Caller Number: *</label>
                    <input type="tel" id="callback-caller-number" required
                           placeholder="e.g., +1234567890">
                </div>
                <div class="form-group">
                    <label for="callback-caller-name">Caller Name:</label>
                    <input type="text" id="callback-caller-name"
                           placeholder="Optional">
                </div>
                <div class="form-group">
                    <label for="callback-preferred-time">Preferred Time:</label>
                    <input type="datetime-local" id="callback-preferred-time">
                    <small>Leave empty for ASAP callback</small>
                </div>
                <div class="form-actions">
                    <button type="button" class="btn btn-secondary" onclick="closeRequestCallbackModal()">Cancel</button>
                    <button type="submit" class="btn btn-success">Request Callback</button>
                </div>
            </form>
        </div>
    `,document.body.appendChild(e),e.style.display=`block`}function Fi(){let e=document.getElementById(`request-callback-modal`);e&&e.remove()}async function Ii(e){e.preventDefault();let t=document.getElementById(`callback-queue-id`)?.value??``,n=document.getElementById(`callback-caller-number`)?.value??``,r=document.getElementById(`callback-caller-name`)?.value??``,i=document.getElementById(`callback-preferred-time`)?.value??``,a={queue_id:t,caller_number:n};r&&(a.caller_name=r),i&&(a.preferred_time=new Date(i).toISOString());try{let e=await(await _(`${h()}/api/callback-queue/request`,{method:`POST`,headers:g(),body:JSON.stringify(a)})).json();e.success?(b(`Callback requested successfully`,`success`),Fi(),M()):b(e.error??`Error requesting callback`,`error`)}catch(e){console.error(`Error requesting callback:`,e),b(`Error requesting callback`,`error`)}}async function Li(e){let t=prompt(`Enter your agent ID/extension:`);if(t)try{let n=await(await _(`${h()}/api/callback-queue/start`,{method:`POST`,headers:g(),body:JSON.stringify({callback_id:e,agent_id:t})})).json();n.success?(b(`Started callback to ${n.caller_number??e}`,`success`),M()):b(n.error??`Error starting callback`,`error`)}catch(e){console.error(`Error starting callback:`,e),b(`Error starting callback`,`error`)}}async function Ri(e,t){let n=``;t||(n=prompt(`Enter reason for failure (optional):`)??``);try{let r=await(await _(`${h()}/api/callback-queue/complete`,{method:`POST`,headers:g(),body:JSON.stringify({callback_id:e,success:t,notes:n})})).json();r.success?(b(t?`Callback completed`:`Callback will be retried`,`success`),M()):b(r.error??`Error completing callback`,`error`)}catch(e){console.error(`Error completing callback:`,e),b(`Error completing callback`,`error`)}}async function zi(e){if(confirm(`Are you sure you want to cancel this callback request?`))try{let t=await(await _(`${h()}/api/callback-queue/cancel`,{method:`POST`,headers:g(),body:JSON.stringify({callback_id:e})})).json();t.success?(b(`Callback cancelled`,`success`),M()):b(t.error??`Error cancelling callback`,`error`)}catch(e){console.error(`Error cancelling callback:`,e),b(`Error cancelling callback`,`error`)}}async function N(){try{let e=h(),[t,n,r]=await Promise.all([_(`${e}/api/mobile-push/devices`,{headers:g()}),_(`${e}/api/mobile-push/statistics`,{headers:g()}),_(`${e}/api/mobile-push/history`,{headers:g()})]),[i,a,o]=await Promise.all([t.json(),n.json(),r.json()]);if(a){let e=e=>document.getElementById(e);e(`push-total-devices`)&&(e(`push-total-devices`).textContent=String(a.total_devices??0)),e(`push-total-users`)&&(e(`push-total-users`).textContent=String(a.total_users??0));let t=a.platforms??{};e(`push-ios-devices`)&&(e(`push-ios-devices`).textContent=String(t.ios??0)),e(`push-android-devices`)&&(e(`push-android-devices`).textContent=String(t.android??0)),e(`push-recent-notifications`)&&(e(`push-recent-notifications`).textContent=String(a.recent_notifications??0))}if(i?.devices){let e=document.getElementById(`mobile-devices-list`);e&&(i.devices.length===0?e.innerHTML=`<tr><td colspan="5" style="text-align: center;">No devices registered</td></tr>`:e.innerHTML=i.devices.map(e=>{let t=new Date(e.registered_at).toLocaleString(),n=new Date(e.last_seen).toLocaleString(),r=``;return r=e.platform===`ios`?`<span class="badge badge-info">iOS</span>`:e.platform===`android`?`<span class="badge badge-success">Android</span>`:`<span class="badge badge-secondary">${y(e.platform)}</span>`,`
                            <tr>
                                <td><strong>${y(e.user_id)}</strong></td>
                                <td>${r}</td>
                                <td><small>${y(t)}</small></td>
                                <td><small>${y(n)}</small></td>
                                <td>
                                    <button class="btn-small btn-primary" onclick="sendTestNotification('${y(e.user_id)}')">Test</button>
                                </td>
                            </tr>
                        `}).join(``))}if(o?.history){let e=document.getElementById(`push-history-list`);e&&(o.history.length===0?e.innerHTML=`<tr><td colspan="5" style="text-align: center;">No notifications sent</td></tr>`:e.innerHTML=o.history.slice(0,50).map(e=>{let t=new Date(e.sent_at).toLocaleString(),n=e.success_count??0,r=e.failure_count??0;return`
                                <tr>
                                    <td>${y(e.user_id)}</td>
                                    <td><strong>${y(e.title)}</strong></td>
                                    <td><small>${y(e.body)}</small></td>
                                    <td><small>${y(t)}</small></td>
                                    <td>
                                        <span class="badge badge-success">${n} sent</span>
                                        ${r>0?`<span class="badge badge-danger">${r} failed</span>`:``}
                                    </td>
                                </tr>
                            `}).join(``))}}catch(e){console.error(`Error loading mobile push data:`,e),b(`Error loading mobile push data`,`error`)}}function Bi(){let e=document.createElement(`div`);e.className=`modal`,e.id=`register-device-modal`,e.innerHTML=`
        <div class="modal-content">
            <span class="close" onclick="closeRegisterDeviceModal()">&times;</span>
            <h2>Register Mobile Device</h2>
            <form id="register-device-form" onsubmit="registerDevice(event)">
                <div class="form-group">
                    <label for="device-user-id">User ID / Extension: *</label>
                    <input type="text" id="device-user-id" required
                           placeholder="e.g., 1001 or user@example.com">
                </div>
                <div class="form-group">
                    <label for="device-token">Device Token: *</label>
                    <textarea id="device-token" required rows="4"
                              placeholder="FCM device registration token"></textarea>
                    <small>Obtain from mobile app after FCM SDK initialization</small>
                </div>
                <div class="form-group">
                    <label for="device-platform">Platform: *</label>
                    <select id="device-platform" required>
                        <option value="">Select Platform</option>
                        <option value="ios">iOS</option>
                        <option value="android">Android</option>
                        <option value="other">Other</option>
                    </select>
                </div>
                <div class="form-actions">
                    <button type="button" class="btn btn-secondary" onclick="closeRegisterDeviceModal()">Cancel</button>
                    <button type="submit" class="btn btn-success">Register Device</button>
                </div>
            </form>
        </div>
    `,document.body.appendChild(e),e.style.display=`block`}function Vi(){let e=document.getElementById(`register-device-modal`);e&&e.remove()}async function Hi(e){e.preventDefault();let t={user_id:document.getElementById(`device-user-id`)?.value??``,device_token:document.getElementById(`device-token`)?.value.trim()??``,platform:document.getElementById(`device-platform`)?.value??``};try{let e=await(await _(`${h()}/api/mobile-push/register`,{method:`POST`,headers:g(),body:JSON.stringify(t)})).json();e.success?(b(`Device registered successfully`,`success`),Vi(),N()):b(e.error??`Error registering device`,`error`)}catch(e){console.error(`Error registering device:`,e),b(`Error registering device`,`error`)}}function Ui(){let e=document.createElement(`div`);e.className=`modal`,e.id=`test-notification-modal`,e.innerHTML=`
        <div class="modal-content">
            <span class="close" onclick="closeTestNotificationModal()">&times;</span>
            <h2>Send Test Notification</h2>
            <form id="test-notification-form" onsubmit="sendTestNotificationForm(event)">
                <div class="form-group">
                    <label for="test-user-id">User ID / Extension: *</label>
                    <input type="text" id="test-user-id" required
                           placeholder="e.g., 1001 or user@example.com">
                </div>
                <div class="form-actions">
                    <button type="button" class="btn btn-secondary" onclick="closeTestNotificationModal()">Cancel</button>
                    <button type="submit" class="btn btn-primary">Send Test</button>
                </div>
            </form>
        </div>
    `,document.body.appendChild(e),e.style.display=`block`}function Wi(){let e=document.getElementById(`test-notification-modal`);e&&e.remove()}function Gi(e){e.preventDefault(),Ki(document.getElementById(`test-user-id`)?.value??``),Wi()}async function Ki(e){try{let t=await(await _(`${h()}/api/mobile-push/test`,{method:`POST`,headers:g(),body:JSON.stringify({user_id:e})})).json();t.success||t.stub_mode?(t.stub_mode?b(`Test notification logged (Firebase not configured)`,`warning`):b(`Test notification sent: ${t.success_count??0} succeeded, ${t.failure_count??0} failed`,`success`),N()):b(t.error??`Error sending test notification`,`error`)}catch(e){console.error(`Error sending test notification:`,e),b(`Error sending test notification`,`error`)}}async function qi(){try{let e=h(),[t,n]=await Promise.all([_(`${e}/api/recording-announcements/statistics`,{headers:g()}),_(`${e}/api/recording-announcements/config`,{headers:g()})]),[r,i]=await Promise.all([t.json(),n.json()]);if(r){let e=e=>document.getElementById(e);e(`announcements-enabled`)&&(e(`announcements-enabled`).textContent=r.enabled?`Enabled`:`Disabled`),e(`announcements-played`)&&(e(`announcements-played`).textContent=String(r.announcements_played??0)),e(`consent-accepted`)&&(e(`consent-accepted`).textContent=String(r.consent_accepted??0)),e(`consent-declined`)&&(e(`consent-declined`).textContent=String(r.consent_declined??0)),e(`announcement-type`)&&(e(`announcement-type`).textContent=r.announcement_type??`N/A`),e(`require-consent`)&&(e(`require-consent`).textContent=r.require_consent?`Yes`:`No`)}if(i){let e=e=>document.getElementById(e);e(`audio-file-path`)&&(e(`audio-file-path`).textContent=i.audio_path??`N/A`),e(`announcement-text`)&&(e(`announcement-text`).textContent=i.announcement_text??`N/A`)}}catch(e){console.error(`Error loading recording announcements data:`,e),b(`Error loading recording announcements data`,`error`)}}async function Ji(){try{let e=await(await _(`${h()}/api/framework/speech-analytics/configs`,{headers:g()})).json(),t=document.getElementById(`speech-analytics-configs-table`);if(!t)return;if(!e.configs||e.configs.length===0){t.innerHTML=`<tr><td colspan="5" class="loading">No extension-specific configurations. Using system defaults.</td></tr>`;return}t.innerHTML=e.configs.map(e=>`
            <tr>
                <td>${y(e.extension)}</td>
                <td>${e.transcription_enabled?`Enabled`:`Disabled`}</td>
                <td>${e.sentiment_enabled?`Enabled`:`Disabled`}</td>
                <td>${e.summarization_enabled?`Enabled`:`Disabled`}</td>
                <td>
                    <button class="btn btn-sm btn-primary" onclick="editSpeechAnalyticsConfig('${y(e.extension)}')">Edit</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteSpeechAnalyticsConfig('${y(e.extension)}')">Delete</button>
                </td>
            </tr>
        `).join(``)}catch(e){console.error(`Error loading speech analytics configs:`,e),b(`Error loading speech analytics configurations`,`error`)}}async function Yi(){try{let e=await _(`${h()}/api/framework/integrations/activity-log`,{headers:g()});if(!e.ok){console.error(`Error loading CRM activity log:`,e.status),b(`Error loading CRM activity log`,`error`);return}let t=await e.json(),n=document.getElementById(`crm-activity-log-table`);if(!n)return;if(!t.activities||t.activities.length===0){n.innerHTML=`<tr><td colspan="5" class="loading">No integration activity yet</td></tr>`;return}n.innerHTML=t.activities.map(e=>{let t=e.status===`success`?`success`:`error`,n=e.status===`success`?`OK`:`FAIL`;return`
                <tr>
                    <td>${y(new Date(e.timestamp).toLocaleString())}</td>
                    <td>${y(e.integration)}</td>
                    <td>${y(e.action)}</td>
                    <td class="${t}">${n} ${y(e.status)}</td>
                    <td>${y(e.details??`-`)}</td>
                </tr>
            `}).join(``)}catch(e){console.error(`Error loading CRM activity log:`,e),b(`Error loading CRM activity log`,`error`)}}async function Xi(){if(confirm(`Clear old activity log entries? This will remove entries older than 30 days.`))try{let e=await(await _(`${h()}/api/framework/integrations/activity-log/clear`,{method:`POST`,headers:g()})).json();e.success?(b(`Cleared ${e.deleted_count??0} old entries`,`success`),Yi()):b(e.error??`Error clearing activity log`,`error`)}catch(e){console.error(`Error clearing CRM activity log:`,e),b(`Error clearing activity log`,`error`)}}async function Zi(){try{let e=await _(`${h()}/api/framework/compliance/soc2/controls`,{headers:g()}),t=e.ok?await e.json():null,n=document.getElementById(`compliance-soc2-count`);if(n&&t){let e=t.controls??[];n.textContent=String(e.length)}}catch(e){console.error(`Error loading compliance data:`,e)}}window.loadFraudAlerts=ki,window.showAddBlockedPatternModal=Ai,window.closeAddBlockedPatternModal=ji,window.addBlockedPattern=Mi,window.deleteBlockedPattern=Ni,window.loadCallbackQueue=M,window.showRequestCallbackModal=Pi,window.closeRequestCallbackModal=Fi,window.requestCallback=Ii,window.startCallback=Li,window.completeCallback=Ri,window.cancelCallback=zi,window.loadMobilePushDevices=N,window.showRegisterDeviceModal=Bi,window.closeRegisterDeviceModal=Vi,window.registerDevice=Hi,window.showTestNotificationModal=Ui,window.closeTestNotificationModal=Wi,window.sendTestNotificationForm=Gi,window.sendTestNotification=Ki,window.loadRecordingAnnouncementsStats=qi,window.loadSpeechAnalyticsConfigs=Ji,window.loadCRMActivityLog=Yi,window.clearCRMActivityLog=Xi,window.loadFraudDetectionData=ki,window.loadMobilePushConfig=N,window.loadRecordingAnnouncements=qi,window.loadComplianceData=Zi;var Qi={completed:`success`,failed:`error`,cancelled:`warning`,busy:`warning`,"no-answer":`warning`};async function $i(){try{let e=await _(`${h()}/api/extensions`,{headers:g()});if(!e.ok)throw Error(`HTTP ${e.status}`);return(await e.json()).extensions??[]}catch(e){return console.error(`Error loading extensions for click-to-dial:`,e),window.currentExtensions??[]}}async function ea(){try{let e=await(await _(`${h()}/api/framework/click-to-dial/configs`,{headers:g()})).json();if(e.error){console.error(`Error loading click-to-dial configs:`,e.error);return}let t=await $i(),n=document.getElementById(`ctd-extension-select`),r=document.getElementById(`ctd-history-extension`);if(n&&t.length>0){n.innerHTML=`<option value="">Select Extension</option>`;for(let e of t){let t=document.createElement(`option`);t.value=e.number,t.textContent=`${e.number} - ${e.name}`,n.appendChild(t)}}else n&&(n.innerHTML=`<option value="">No extensions available</option>`);if(r&&t.length>0){r.innerHTML=`<option value="">All Extensions</option>`;for(let e of t){let t=document.createElement(`option`);t.value=e.number,t.textContent=`${e.number} - ${e.name}`,r.appendChild(t)}}else r&&(r.innerHTML=`<option value="">No extensions available</option>`);let i=document.getElementById(`ctd-configs-table`);if(!i)return;if(!e.configs||e.configs.length===0){i.innerHTML=`<tr><td colspan="6" style="text-align: center;">No configurations found. Configure extensions above.</td></tr>`;return}i.innerHTML=e.configs.map(e=>`
            <tr>
                <td>${y(e.extension)}</td>
                <td><span class="status-badge ${e.enabled?`success`:`error`}">${e.enabled?`Enabled`:`Disabled`}</span></td>
                <td>${e.default_caller_id?y(e.default_caller_id):`-`}</td>
                <td>${e.auto_answer?`Yes`:`No`}</td>
                <td>${e.browser_notification?`Yes`:`No`}</td>
                <td>
                    <button class="btn btn-sm btn-primary" onclick="editClickToDialConfig('${y(e.extension)}')">Edit</button>
                </td>
            </tr>
        `).join(``)}catch(e){console.error(`Error loading click-to-dial configs:`,e),e instanceof Error&&x(e,`Loading click-to-dial configurations`)}}function ta(e){let t=document.getElementById(`ctd-config-section`),n=document.getElementById(`ctd-no-extension`);t&&n&&(t.style.display=e?`block`:`none`,n.style.display=e?`none`:`block`)}async function na(){let e=document.getElementById(`ctd-extension-select`)?.value;if(!e){ta(!1);return}try{let t=await(await _(`${h()}/api/framework/click-to-dial/config/${e}`,{headers:g()})).json();if(t.error){console.error(`Error loading config:`,t.error);let n=document.getElementById(`ctd-current-extension`);n&&(n.textContent=e);let r=document.getElementById(`ctd-enabled`);r&&(r.checked=!0);let i=document.getElementById(`ctd-caller-id`);i&&(i.value=``);let a=document.getElementById(`ctd-auto-answer`);a&&(a.checked=!1);let o=document.getElementById(`ctd-browser-notification`);o&&(o.checked=!0)}else{let n=document.getElementById(`ctd-current-extension`);n&&(n.textContent=e);let r=document.getElementById(`ctd-enabled`);r&&(r.checked=t.config.enabled);let i=document.getElementById(`ctd-caller-id`);i&&(i.value=t.config.default_caller_id??``);let a=document.getElementById(`ctd-auto-answer`);a&&(a.checked=t.config.auto_answer);let o=document.getElementById(`ctd-browser-notification`);o&&(o.checked=t.config.browser_notification)}ta(!0)}catch(e){console.error(`Error loading click-to-dial config:`,e),e instanceof Error&&x(e,`Loading click-to-dial configuration`)}}async function ra(e){e.preventDefault();let t=document.getElementById(`ctd-current-extension`)?.textContent;if(!t){b(`No extension selected`,`error`);return}let n={enabled:document.getElementById(`ctd-enabled`)?.checked??!1,default_caller_id:document.getElementById(`ctd-caller-id`)?.value.trim()||null,auto_answer:document.getElementById(`ctd-auto-answer`)?.checked??!1,browser_notification:document.getElementById(`ctd-browser-notification`)?.checked??!1};try{let e=await(await _(`${h()}/api/framework/click-to-dial/config/${t}`,{method:`POST`,headers:g(),body:JSON.stringify(n)})).json();e.error?b(`Error: ${e.error}`,`error`):(b(`Configuration saved successfully`,`success`),ea())}catch(e){console.error(`Error saving config:`,e),e instanceof Error&&x(e,`Saving click-to-dial configuration`),b(`Error saving configuration`,`error`)}}async function ia(e){let t=document.getElementById(`ctd-extension-select`);if(t){t.value=e,await na();let n=document.getElementById(`ctd-config-section`);n&&n.scrollIntoView({behavior:`smooth`})}}async function aa(){let e=document.getElementById(`ctd-extension-select`)?.value,t=document.getElementById(`ctd-phone-number`),n=t?.value.trim();if(!e){b(`Please select an extension`,`error`);return}if(!n){b(`Please enter a phone number`,`error`);return}try{let r=await(await _(`${h()}/api/framework/click-to-dial/call/${e}`,{method:`POST`,headers:g(),body:JSON.stringify({destination:n})})).json();r.error?b(`Error: ${r.error}`,`error`):(b(`Call initiated from extension ${e} to ${n}`,`success`),t&&(t.value=``),setTimeout(()=>oa(),1e3))}catch(e){console.error(`Error initiating call:`,e),e instanceof Error&&x(e,`Initiating click-to-dial call`),b(`Error initiating call`,`error`)}}async function oa(){let e=document.getElementById(`ctd-history-extension`)?.value,t=document.getElementById(`ctd-history-table`);if(t){if(!e){t.innerHTML=`<tr><td colspan="5" style="text-align: center;">Select an extension to view history</td></tr>`;return}try{let n=await(await _(`${h()}/api/framework/click-to-dial/history/${e}`,{headers:g()})).json();if(n.error){t.innerHTML=`<tr><td colspan="5" style="text-align: center;">Error: ${y(n.error)}</td></tr>`;return}if(!n.history||n.history.length===0){t.innerHTML=`<tr><td colspan="5" style="text-align: center;">No call history found</td></tr>`;return}t.innerHTML=n.history.map(e=>{let t=new Date(e.timestamp).toLocaleString(),n=e.duration?`${e.duration}s`:`-`,r=Qi[e.status]??`warning`;return`
                <tr>
                    <td>${y(t)}</td>
                    <td>${y(e.extension)}</td>
                    <td>${y(e.destination)}</td>
                    <td>${n}</td>
                    <td><span class="status-badge ${r}">${y(e.status)}</span></td>
                </tr>
            `}).join(``)}catch(e){console.error(`Error loading history:`,e),e instanceof Error&&x(e,`Loading click-to-dial history`),t&&(t.innerHTML=`<tr><td colspan="5" style="text-align: center;">Error loading history</td></tr>`)}}}async function sa(){try{let e=await(await _(`${h()}/api/webrtc/phone-config`,{headers:g()})).json();if(e.success){let t=document.getElementById(`webrtc-phone-extension`);t&&(t.value=e.extension??(typeof DEFAULT_WEBRTC_EXTENSION<`u`?DEFAULT_WEBRTC_EXTENSION:``)),typeof initWebRTCPhone==`function`&&initWebRTCPhone()}else console.error(`Failed to load WebRTC phone config:`,e.error)}catch(e){console.error(`Error loading WebRTC phone config:`,e)}}async function ca(e){e.preventDefault();let t=document.getElementById(`webrtc-phone-extension`)?.value.trim()??``;if(!t){b(`Please enter an extension`,`error`);return}try{let e=await(await _(`${h()}/api/webrtc/phone-config`,{method:`POST`,headers:g(),body:JSON.stringify({extension:t})})).json();e.success?(b(`Phone extension saved successfully! Reloading phone...`,`success`),typeof initWebRTCPhone==`function`&&initWebRTCPhone()):b(`Error: ${e.error??`Failed to save phone extension`}`,`error`)}catch(e){console.error(`Error saving WebRTC phone config:`,e),b(`Error: ${e instanceof Error?e.message:String(e)}`,`error`)}}window.loadClickToDialConfigs=ea,window.toggleClickToDialConfigSections=ta,window.loadClickToDialConfig=na,window.saveClickToDialConfig=ra,window.editClickToDialConfig=ia,window.initiateClickToDial=aa,window.loadClickToDialHistory=oa,window.loadWebRTCPhoneConfig=sa,window.saveWebRTCPhoneConfig=ca,window.loadClickToDialTab=ea;async function P(){try{let e=await(await _(`${h()}/api/framework/nomadic-e911/sites`,{headers:g()})).json(),t=document.getElementById(`e911-sites-table`);if(!t)return;if(!e.sites||e.sites.length===0){t.innerHTML=`<tr><td colspan="5" class="loading">No E911 sites configured</td></tr>`;return}t.innerHTML=e.sites.map(e=>`
            <tr>
                <td>${y(e.site_name)}</td>
                <td>${y(e.street_address)}, ${y(e.city)}, ${y(e.state)} ${y(e.postal_code)}</td>
                <td>${y(e.ip_range_start??``)} - ${y(e.ip_range_end??``)}</td>
                <td>${y(e.psap_number??`Default`)}</td>
                <td>
                    <button class="btn btn-sm btn-primary" onclick="editE911Site(${e.id})">Edit</button>
                    <button class="btn btn-sm btn-danger" onclick="deleteE911Site(${e.id})">Delete</button>
                </td>
            </tr>
        `).join(``)}catch(e){console.error(`Error loading E911 sites:`,e),b(`Error loading E911 sites`,`error`)}}async function la(){try{let e=await(await _(`${h()}/api/framework/nomadic-e911/locations`,{headers:g()})).json(),t=document.getElementById(`extension-locations-table`);if(!t)return;if(!e.locations||e.locations.length===0){t.innerHTML=`<tr><td colspan="5" class="loading">No location data available</td></tr>`;return}t.innerHTML=e.locations.map(e=>`
            <tr>
                <td>${y(e.extension)}</td>
                <td>${y(e.site_name??`Unknown`)} - ${y(e.address??`N/A`)}</td>
                <td>${y(e.detection_method??`N/A`)}</td>
                <td>${e.last_updated?new Date(e.last_updated).toLocaleString():`N/A`}</td>
                <td>
                    <button class="btn btn-sm btn-primary" onclick="updateExtensionLocation('${y(e.extension)}')">Update</button>
                </td>
            </tr>
        `).join(``)}catch(e){console.error(`Error loading extension locations:`,e),b(`Error loading extension locations`,`error`)}}async function ua(){let e=document.getElementById(`location-history-extension`)?.value??``,t=e?`/api/framework/nomadic-e911/history/${e}`:`/api/framework/nomadic-e911/history`;try{let e=await(await _(`${h()}${t}`,{headers:g()})).json(),n=document.getElementById(`location-history-table`);if(!n)return;if(!e.history||e.history.length===0){n.innerHTML=`<tr><td colspan="5" class="loading">No location history available</td></tr>`;return}n.innerHTML=e.history.map(e=>`
            <tr>
                <td>${y(new Date(e.timestamp).toLocaleString())}</td>
                <td>${y(e.extension)}</td>
                <td>${y(e.site_name??`N/A`)}</td>
                <td>${y(e.detection_method??`N/A`)}</td>
                <td>${y(e.ip_address??`N/A`)}</td>
            </tr>
        `).join(``)}catch(e){console.error(`Error loading location history:`,e),b(`Error loading location history`,`error`)}}function F(){let e=document.getElementById(`e911-site-modal`);e&&e.remove()}function da(e){return`
        <div id="e911-site-modal" class="modal" style="display: flex; align-items: center;
             justify-content: center;">
            <div class="modal-content" style="max-width: 600px;">
                <div class="modal-header">
                    <h3>${e?`Edit`:`Add`} E911 Site</h3>
                    <span class="close" onclick="removeE911SiteModal()">&times;</span>
                </div>
                <form id="e911-site-form">
                    ${e?`<input type="hidden" name="site_id" value="${e.id}">`:``}
                    <div class="form-group">
                        <label>Site Name:</label>
                        <input type="text" name="site_name" required class="form-control"
                            value="${y(e?.site_name??``)}">
                    </div>
                    <div class="form-group">
                        <label>Street Address:</label>
                        <input type="text" name="street_address" class="form-control"
                            value="${y(e?.street_address??``)}">
                    </div>
                    <div class="form-group">
                        <label>City:</label>
                        <input type="text" name="city" class="form-control"
                            value="${y(e?.city??``)}">
                    </div>
                    <div class="form-group">
                        <label>State:</label>
                        <input type="text" name="state" class="form-control"
                            value="${y(e?.state??``)}">
                    </div>
                    <div class="form-group">
                        <label>Postal Code:</label>
                        <input type="text" name="postal_code" class="form-control"
                            value="${y(e?.postal_code??``)}">
                    </div>
                    <div class="form-group">
                        <label>Country:</label>
                        <input type="text" name="country" class="form-control"
                            value="${y(e?.country??`USA`)}" placeholder="USA">
                    </div>
                    <div class="form-group">
                        <label>IP Range Start:</label>
                        <input type="text" name="ip_range_start" required class="form-control"
                            value="${y(e?.ip_range_start??``)}"
                            placeholder="192.168.1.0">
                    </div>
                    <div class="form-group">
                        <label>IP Range End:</label>
                        <input type="text" name="ip_range_end" required class="form-control"
                            value="${y(e?.ip_range_end??``)}"
                            placeholder="192.168.1.255">
                    </div>
                    <div class="form-group">
                        <label>Emergency Trunk:</label>
                        <input type="text" name="emergency_trunk" class="form-control"
                            value="${y(e?.emergency_trunk??``)}">
                    </div>
                    <div class="form-group">
                        <label>PSAP Number:</label>
                        <input type="text" name="psap_number" class="form-control"
                            value="${y(e?.psap_number??``)}"
                            placeholder="Default PSAP">
                    </div>
                    <div class="form-group">
                        <label>ELIN:</label>
                        <input type="text" name="elin" class="form-control"
                            value="${y(e?.elin??``)}">
                        <small>Emergency Location Information Number</small>
                    </div>
                    <div class="form-group">
                        <label>Building:</label>
                        <input type="text" name="building" class="form-control"
                            value="${y(e?.building??``)}">
                    </div>
                    <div class="form-group">
                        <label>Floor:</label>
                        <input type="text" name="floor" class="form-control"
                            value="${y(e?.floor??``)}">
                    </div>
                    <div class="modal-actions">
                        <button type="submit" class="btn btn-primary">
                            ${e?`Update`:`Create`} Site
                        </button>
                        <button type="button" class="btn btn-secondary"
                            onclick="removeE911SiteModal()">Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    `}async function fa(e){let t=new FormData(e),n=t.get(`site_id`),r={};for(let[e,n]of t.entries())e!==`site_id`&&String(n).trim()&&(r[e]=String(n).trim());try{let e=h(),t=await(await _(n?`${e}/api/framework/nomadic-e911/sites/${n}`:`${e}/api/framework/nomadic-e911/create-site`,{method:n?`PUT`:`POST`,headers:g(),body:JSON.stringify(r)})).json();t.success?(b(`E911 site saved successfully`,`success`),F(),await P()):b(t.error??`Error saving E911 site`,`error`)}catch(e){console.error(`Error saving E911 site:`,e),b(`Error saving E911 site`,`error`)}}function pa(){F(),document.body.insertAdjacentHTML(`beforeend`,da());let e=document.getElementById(`e911-site-form`);e.onsubmit=t=>{t.preventDefault(),fa(e)}}function ma(e){_(`${h()}/api/framework/nomadic-e911/sites`,{headers:g()}).then(async t=>{let n=(await t.json()).sites?.find(t=>t.id===e);if(!n){b(`Site not found`,`error`);return}F(),document.body.insertAdjacentHTML(`beforeend`,da(n));let r=document.getElementById(`e911-site-form`);r.onsubmit=e=>{e.preventDefault(),fa(r)}}).catch(()=>{b(`Error loading site details`,`error`)})}async function ha(e){if(confirm(`Delete E911 site ${e}?`))try{let t=await(await _(`${h()}/api/framework/nomadic-e911/sites/${e}`,{method:`DELETE`,headers:g()})).json();t.success?(b(`E911 site deleted`,`success`),await P()):b(t.error??`Error deleting site`,`error`)}catch(e){console.error(`Error deleting E911 site:`,e),b(`Error deleting E911 site`,`error`)}}function I(){let e=document.getElementById(`location-update-modal`);e&&e.remove()}function ga(e){return`
        <div id="location-update-modal" class="modal" style="display: flex;
             align-items: center; justify-content: center;">
            <div class="modal-content" style="max-width: 600px;">
                <div class="modal-header">
                    <h3>Update Extension Location</h3>
                    <span class="close" onclick="removeLocationModal()">&times;</span>
                </div>
                <form id="location-update-form">
                    <div class="form-group">
                        <label>Extension:</label>
                        <input type="text" name="extension" required class="form-control"
                            value="${y(e??``)}"
                            ${e?`readonly`:``}>
                    </div>
                    <div class="form-group">
                        <label>Location Name:</label>
                        <input type="text" name="location_name" class="form-control"
                            placeholder="Main Office">
                    </div>
                    <div class="form-group">
                        <label>IP Address:</label>
                        <input type="text" name="ip_address" class="form-control"
                            placeholder="192.168.1.100">
                    </div>
                    <div class="form-group">
                        <label>Street Address:</label>
                        <input type="text" name="street_address" class="form-control">
                    </div>
                    <div class="form-group">
                        <label>City:</label>
                        <input type="text" name="city" class="form-control">
                    </div>
                    <div class="form-group">
                        <label>State:</label>
                        <input type="text" name="state" class="form-control">
                    </div>
                    <div class="form-group">
                        <label>Postal Code:</label>
                        <input type="text" name="postal_code" class="form-control">
                    </div>
                    <div class="form-group">
                        <label>Country:</label>
                        <input type="text" name="country" class="form-control"
                            placeholder="USA">
                    </div>
                    <div class="form-group">
                        <label>Building:</label>
                        <input type="text" name="building" class="form-control">
                    </div>
                    <div class="form-group">
                        <label>Floor:</label>
                        <input type="text" name="floor" class="form-control">
                    </div>
                    <div class="form-group">
                        <label>Room:</label>
                        <input type="text" name="room" class="form-control">
                    </div>
                    <div class="modal-actions">
                        <button type="submit" class="btn btn-primary">Update Location</button>
                        <button type="button" class="btn btn-secondary"
                            onclick="removeLocationModal()">Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    `}async function _a(e){let t=new FormData(e),n=String(t.get(`extension`)??``).trim();if(!n){b(`Extension is required`,`error`);return}let r={};for(let[e,n]of t.entries())e!==`extension`&&String(n).trim()&&(r[e]=String(n).trim());try{let e=await(await _(`${h()}/api/framework/nomadic-e911/update-location/${encodeURIComponent(n)}`,{method:`POST`,headers:g(),body:JSON.stringify(r)})).json();e.success?(b(`Location updated for extension ${n}`,`success`),I(),await la()):b(e.error??`Error updating location`,`error`)}catch(e){console.error(`Error updating location:`,e),b(`Error updating location`,`error`)}}function va(){I(),document.body.insertAdjacentHTML(`beforeend`,ga());let e=document.getElementById(`location-update-form`);e.onsubmit=t=>{t.preventDefault(),_a(e)}}function ya(e){I(),document.body.insertAdjacentHTML(`beforeend`,ga(e));let t=document.getElementById(`location-update-form`);t.onsubmit=e=>{e.preventDefault(),_a(t)}}function L(){let e=document.getElementById(`speech-config-modal`);e&&e.remove()}function ba(e,t){return`
        <div id="speech-config-modal" class="modal" style="display: flex;
             align-items: center; justify-content: center;">
            <div class="modal-content" style="max-width: 600px;">
                <div class="modal-header">
                    <h3>${t?`Edit`:`Add`} Speech Analytics Config</h3>
                    <span class="close" onclick="removeSpeechConfigModal()">&times;</span>
                </div>
                <form id="speech-config-form">
                    <div class="form-group">
                        <label>Extension:</label>
                        <input type="text" name="extension" required class="form-control"
                            value="${y(e??``)}"
                            ${e?`readonly`:``}>
                    </div>
                    <div class="form-group">
                        <label>
                            <input type="checkbox" name="enabled"
                                ${t?.enabled===!1?``:`checked`}>
                            Enabled
                        </label>
                    </div>
                    <div class="form-group">
                        <label>
                            <input type="checkbox" name="transcription_enabled"
                                ${t?.transcription_enabled===!1?``:`checked`}>
                            Transcription
                        </label>
                    </div>
                    <div class="form-group">
                        <label>
                            <input type="checkbox" name="sentiment_enabled"
                                ${t?.sentiment_enabled===!1?``:`checked`}>
                            Sentiment Analysis
                        </label>
                    </div>
                    <div class="form-group">
                        <label>
                            <input type="checkbox" name="summarization_enabled"
                                ${t?.summarization_enabled===!1?``:`checked`}>
                            Call Summarization
                        </label>
                    </div>
                    <div class="form-group">
                        <label>Keywords (comma-separated):</label>
                        <input type="text" name="keywords" class="form-control"
                            value="${y(t?.keywords??``)}"
                            placeholder="urgent, escalation, complaint">
                    </div>
                    <div class="form-group">
                        <label>Alert Threshold (0.0 - 1.0):</label>
                        <input type="number" name="alert_threshold" class="form-control"
                            value="${t?.alert_threshold??.7}"
                            min="0" max="1" step="0.1">
                    </div>
                    <div class="modal-actions">
                        <button type="submit" class="btn btn-primary">
                            ${t?`Update`:`Create`} Config
                        </button>
                        <button type="button" class="btn btn-secondary"
                            onclick="removeSpeechConfigModal()">Cancel</button>
                    </div>
                </form>
            </div>
        </div>
    `}async function xa(e){let t=new FormData(e),n=String(t.get(`extension`)??``).trim();if(!n){b(`Extension is required`,`error`);return}let r={enabled:t.get(`enabled`)===`on`,transcription_enabled:t.get(`transcription_enabled`)===`on`,sentiment_enabled:t.get(`sentiment_enabled`)===`on`,summarization_enabled:t.get(`summarization_enabled`)===`on`,keywords:String(t.get(`keywords`)??``).trim(),alert_threshold:parseFloat(String(t.get(`alert_threshold`)??`0.7`))};try{let e=await(await _(`${h()}/api/framework/speech-analytics/config/${encodeURIComponent(n)}`,{method:`POST`,headers:g(),body:JSON.stringify(r)})).json();e.success?(b(`Speech analytics config saved`,`success`),L()):b(e.error??`Error saving config`,`error`)}catch(e){console.error(`Error saving speech analytics config:`,e),b(`Error saving speech analytics config`,`error`)}}function Sa(){L(),document.body.insertAdjacentHTML(`beforeend`,ba());let e=document.getElementById(`speech-config-form`);e.onsubmit=t=>{t.preventDefault(),xa(e)}}function Ca(e){L(),document.body.insertAdjacentHTML(`beforeend`,ba(e));let t=document.getElementById(`speech-config-form`);t.onsubmit=e=>{e.preventDefault(),xa(t)}}async function wa(e){if(confirm(`Delete speech analytics config for extension ${e}?`))try{let t=await(await _(`${h()}/api/framework/speech-analytics/config/${encodeURIComponent(e)}`,{method:`DELETE`,headers:g()})).json();t.success?b(`Speech analytics config deleted`,`success`):b(t.error??`Error deleting config`,`error`)}catch(e){console.error(`Error deleting speech analytics config:`,e),b(`Error deleting speech analytics config`,`error`)}}window.loadE911Sites=P,window.loadExtensionLocations=la,window.loadLocationHistory=ua,window.showAddE911SiteModal=pa,window.editE911Site=ma,window.deleteE911Site=ha,window.removeE911SiteModal=F,window.showUpdateLocationModal=va,window.updateExtensionLocation=ya,window.removeLocationModal=I,window.showAddSpeechAnalyticsConfigModal=Sa,window.editSpeechAnalyticsConfig=Ca,window.deleteSpeechAnalyticsConfig=wa,window.removeSpeechConfigModal=L,window.loadNomadicE911Data=function(){P(),la()};async function Ta(){await Promise.all([R(),Ea(),Da(),Oa(),ka()])}async function R(){try{let e=await _(`${h()}/api/framework/sbc/statistics`,{headers:g()},1e4);if(!e.ok)throw Error(`HTTP ${e.status}`);let t=await e.json();z(`sbc-active-sessions`,String(t.active_sessions)),z(`sbc-total-sessions`,String(t.total_sessions)),z(`sbc-blocked-requests`,String(t.blocked_requests)),z(`sbc-relayed-media`,String(t.relayed_media_mb)),z(`sbc-bandwidth-util`,`${t.bandwidth_utilization_pct}%`),z(`sbc-rate-violations`,String(t.rate_limit_violations)),z(`sbc-cac-rejections`,String(t.cac_rejections)),z(`sbc-port-pool`,`${t.relay_port_pool_size}/${t.relay_port_pool_total}`)}catch(e){console.error(`Error loading SBC statistics:`,e),b(`Failed to load SBC statistics: ${e instanceof Error?e.message:String(e)}`,`error`)}}async function Ea(){try{let e=await _(`${h()}/api/framework/sbc/config`,{headers:g()},1e4);if(!e.ok)throw Error(`HTTP ${e.status}`);let t=await e.json();B(`sbc-cfg-enabled`,String(t.enabled)),B(`sbc-cfg-topology-hiding`,String(t.topology_hiding)),B(`sbc-cfg-media-relay`,String(t.media_relay)),B(`sbc-cfg-stun`,String(t.stun_enabled)),V(`sbc-cfg-public-ip`,t.public_ip),V(`sbc-cfg-max-calls`,String(t.max_calls)),V(`sbc-cfg-max-bandwidth`,String(t.max_bandwidth)),V(`sbc-cfg-rate-limit`,String(t.rate_limit))}catch(e){console.error(`Error loading SBC config:`,e)}}async function Da(){try{let e=await _(`${h()}/api/framework/sbc/relays`,{headers:g()},1e4);if(!e.ok)throw Error(`HTTP ${e.status}`);let t=await e.json(),n=document.getElementById(`sbc-relays-list`);if(!n)return;let r=Object.values(t.relays??{});if(r.length===0){n.innerHTML=`<tr><td colspan="7" style="text-align: center;">No active relays</td></tr>`;return}n.innerHTML=r.map(e=>`
      <tr>
        <td><code>${y(e.call_id)}</code></td>
        <td>${y(e.codec)}</td>
        <td>${e.rtp_port}</td>
        <td>${e.rtcp_port}</td>
        <td>${y(e.relay_ip)}</td>
        <td>${y(e.allocated_at??`-`)}</td>
        <td>
          <button class="btn btn-danger btn-small" onclick="terminateSBCRelay('${y(e.call_id)}')">Terminate</button>
        </td>
      </tr>
    `).join(``)}catch(e){console.error(`Error loading SBC relays:`,e);let t=document.getElementById(`sbc-relays-list`);t&&(t.innerHTML=`<tr><td colspan="7" style="text-align: center;">Error loading relays</td></tr>`)}}async function Oa(){try{let e=await _(`${h()}/api/framework/sbc/blacklist`,{headers:g()},1e4);if(!e.ok)throw Error(`HTTP ${e.status}`);La(`sbc-blacklist-table`,(await e.json()).blacklist,`removeSBCBlacklist`)}catch(e){console.error(`Error loading SBC blacklist:`,e)}}async function ka(){try{let e=await _(`${h()}/api/framework/sbc/whitelist`,{headers:g()},1e4);if(!e.ok)throw Error(`HTTP ${e.status}`);La(`sbc-whitelist-table`,(await e.json()).whitelist,`removeSBCWhitelist`)}catch(e){console.error(`Error loading SBC whitelist:`,e)}}async function Aa(){try{let e=h(),t={enabled:H(`sbc-cfg-enabled`)===`true`,topology_hiding:H(`sbc-cfg-topology-hiding`)===`true`,media_relay:H(`sbc-cfg-media-relay`)===`true`,stun_enabled:H(`sbc-cfg-stun`)===`true`,public_ip:U(`sbc-cfg-public-ip`),max_calls:parseInt(U(`sbc-cfg-max-calls`)||`1000`,10),max_bandwidth:parseInt(U(`sbc-cfg-max-bandwidth`)||`100000`,10),rate_limit:parseInt(U(`sbc-cfg-rate-limit`)||`100`,10)},n=await _(`${e}/api/framework/sbc/config`,{method:`PUT`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify(t)},1e4);if(!n.ok)throw Error(`HTTP ${n.status}`);b(`Warden SBC configuration saved`,`success`),await R()}catch(e){console.error(`Error saving SBC config:`,e),b(`Failed to save SBC configuration`,`error`)}}async function ja(e){if(confirm(`Terminate relay for call ${e}?`))try{let t=await _(`${h()}/api/framework/sbc/relay/${encodeURIComponent(e)}`,{method:`DELETE`,headers:g()},1e4);if(!t.ok)throw Error(`HTTP ${t.status}`);b(`Relay terminated`,`success`),await Da(),await R()}catch(e){console.error(`Error terminating relay:`,e),b(`Failed to terminate relay`,`error`)}}async function Ma(){let e=document.getElementById(`sbc-blacklist-ip`),t=e?.value?.trim();if(!t){b(`Enter an IP address`,`warning`);return}try{let n=await _(`${h()}/api/framework/sbc/blacklist`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify({ip:t})},1e4);if(!n.ok)throw Error(`HTTP ${n.status}`);e&&(e.value=``),b(`${t} added to blacklist`,`success`),await Oa(),await R()}catch(e){console.error(`Error adding to blacklist:`,e),b(`Failed to add to blacklist`,`error`)}}async function Na(e){try{let t=await _(`${h()}/api/framework/sbc/blacklist/${encodeURIComponent(e)}`,{method:`DELETE`,headers:g()},1e4);if(!t.ok)throw Error(`HTTP ${t.status}`);b(`${e} removed from blacklist`,`success`),await Oa(),await R()}catch(e){console.error(`Error removing from blacklist:`,e),b(`Failed to remove from blacklist`,`error`)}}async function Pa(){let e=document.getElementById(`sbc-whitelist-ip`),t=e?.value?.trim();if(!t){b(`Enter an IP address`,`warning`);return}try{let n=await _(`${h()}/api/framework/sbc/whitelist`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify({ip:t})},1e4);if(!n.ok)throw Error(`HTTP ${n.status}`);e&&(e.value=``),b(`${t} added to whitelist`,`success`),await ka(),await R()}catch(e){console.error(`Error adding to whitelist:`,e),b(`Failed to add to whitelist`,`error`)}}async function Fa(e){try{let t=await _(`${h()}/api/framework/sbc/whitelist/${encodeURIComponent(e)}`,{method:`DELETE`,headers:g()},1e4);if(!t.ok)throw Error(`HTTP ${t.status}`);b(`${e} removed from whitelist`,`success`),await ka(),await R()}catch(e){console.error(`Error removing from whitelist:`,e),b(`Failed to remove from whitelist`,`error`)}}async function Ia(){let e=U(`sbc-nat-local-ip`),t=U(`sbc-nat-public-ip`);if(!e||!t){b(`Enter both local and public IP addresses`,`warning`);return}try{let n=await _(`${h()}/api/framework/sbc/nat-detect`,{method:`POST`,headers:{...g(),"Content-Type":`application/json`},body:JSON.stringify({local_ip:e,public_ip:t})},15e3);if(!n.ok)throw Error(`HTTP ${n.status}`);let r=await n.json(),i=document.getElementById(`sbc-nat-result`),a=document.getElementById(`sbc-nat-type`);i&&(i.style.display=`block`),a&&(a.textContent=r.nat_type),b(`NAT type detected: ${r.nat_type}`,`success`)}catch(e){console.error(`Error detecting NAT:`,e),b(`NAT detection failed`,`error`)}}function z(e,t){let n=document.getElementById(e);n&&(n.textContent=t)}function B(e,t){let n=document.getElementById(e);n&&(n.value=t)}function V(e,t){let n=document.getElementById(e);n&&(n.value=t)}function H(e){return document.getElementById(e)?.value??``}function U(e){return document.getElementById(e)?.value?.trim()??``}function La(e,t,n){let r=document.getElementById(e);if(r){if(t.length===0){r.innerHTML=`<tr><td colspan="2" style="text-align: center;">No entries</td></tr>`;return}r.innerHTML=t.map(e=>`
    <tr>
      <td><code>${y(e)}</code></td>
      <td>
        <button class="btn btn-danger btn-small" onclick="${n}('${y(e)}')">Remove</button>
      </td>
    </tr>
  `).join(``)}}window.loadSBCData=Ta,window.saveSBCConfig=Aa,window.terminateSBCRelay=ja,window.addSBCBlacklist=Ma,window.removeSBCBlacklist=Na,window.addSBCWhitelist=Pa,window.removeSBCWhitelist=Fa,window.detectSBCNat=Ia,window.fetchWithTimeout=_,window.getAuthHeaders=g,window.getApiBaseUrl=h,window.DEFAULT_FETCH_TIMEOUT=He,window.store=v,window.showNotification=b,window.displayError=x,window.setSuppressErrorNotifications=nt,window.showTab=S,window.switchTab=S,window.initializeTabs=lt,window.escapeHtml=y,window.copyToClipboard=Ue,window.formatDate=We,window.truncate=Ge,window.getDuration=Ke,window.getStatusBadge=qe,window.getHealthBadge=Je,window.getPriorityBadge=Ye,window.getQualityClass=Xe,window.getScheduleDescription=Ze,window.downloadLicense=Qe,window.executeBatched=ut,window.refreshAllData=dt;var W=`/admin/login.html`;async function Ra(){if(debugLog(`Initializing user context...`),!localStorage.getItem(`pbx_token`)){debugLog(`No authentication token found, redirecting to login...`),window.location.replace(W);return}try{let e=await _(`${h()}/api/extensions`,{headers:g()},5e3);if(e.status===401||e.status===403){debugLog(`Authentication token is invalid, redirecting to login...`),localStorage.removeItem(`pbx_token`),localStorage.removeItem(`pbx_extension`),localStorage.removeItem(`pbx_is_admin`),localStorage.removeItem(`pbx_name`),window.location.replace(W);return}if(!e.ok)throw Error(`HTTP ${e.status}`)}catch(e){console.error(`Error verifying authentication:`,e),b(`Unable to verify authentication - server may be starting up`,`error`)}let e=localStorage.getItem(`pbx_extension`),t=localStorage.getItem(`pbx_is_admin`)===`true`,n=localStorage.getItem(`pbx_name`)||`User`;if(!e){debugLog(`No extension number found, redirecting to login...`),window.location.replace(W);return}v.set(`currentUser`,{number:e,is_admin:t,name:n}),debugLog(`User context initialized:`,{number:e,is_admin:t,name:n}),t?(debugLog(`Admin user - showing dashboard tab`),S(`dashboard`)):(debugLog(`Regular user - showing webrtc-phone tab`),S(`webrtc-phone`)),debugLog(`User context initialization complete`)}function za(){let e=document.querySelectorAll(`form[data-ajax]`);for(let t of e)t.addEventListener(`submit`,e=>{e.preventDefault(),debugLog(`Ajax form submitted:`,t.id)})}function Ba(){let e=document.getElementById(`logout-button`);e&&e.addEventListener(`click`,async()=>{let e=localStorage.getItem(`pbx_token`),t=g();localStorage.removeItem(`pbx_token`),localStorage.removeItem(`pbx_extension`),localStorage.removeItem(`pbx_is_admin`),localStorage.removeItem(`pbx_name`),localStorage.removeItem(`pbx_current_extension`);try{e&&await fetch(`${h()}/api/auth/logout`,{method:`POST`,headers:t})}catch(e){console.error(`Logout API error:`,e)}window.location.href=W})}async function Va(){let e=document.getElementById(`connection-status`);if(!e){console.error(`Connection status badge element not found`);return}try{if((await _(`${h()}/api/status`,{headers:g()},5e3)).ok)e.textContent=`Connected`,e.classList.remove(`connecting`,`disconnected`),e.classList.add(`connected`);else throw Error(`Connection failed`)}catch(t){console.error(`Connection check failed:`,t),e.textContent=`Disconnected`,e.classList.remove(`connecting`,`connected`),e.classList.add(`disconnected`)}}document.addEventListener(`DOMContentLoaded`,async()=>{debugLog(`DOMContentLoaded event fired - starting initialization`),Va(),setInterval(Va,1e4),debugLog(`Initializing tabs, forms, and logout`),lt(),za(),Ba(),ft();try{await Ra(),debugLog(`User context initialization complete`)}catch(e){console.error(`User context initialization failed:`,e)}debugLog(`Page initialization complete`)});async function Ha(){try{let e=await fetch(`${API_BASE}/api/auto-attendant/config`,{headers:pbxAuthHeaders()});if(e.ok){let t=await e.json();document.getElementById(`aa-enabled`).checked=t.enabled??!1,document.getElementById(`aa-extension`).value=t.extension??`0`,document.getElementById(`aa-timeout`).value=t.timeout??10,document.getElementById(`aa-max-retries`).value=t.max_retries??3}await to(),await G(),await Ua()}catch(e){console.error(`Error loading auto attendant config:`,e),showNotification(`Failed to load auto attendant configuration`,`error`)}}async function Ua(){try{let e=await fetch(`${API_BASE}/api/auto-attendant/prompts`,{headers:pbxAuthHeaders()});if(!e.ok){debugWarn(`Failed to load prompts, using defaults`);return}let t=await e.json(),n=t.prompts??{},r=t.company_name??``,i=document.getElementById(`aa-company-name`);i&&(i.value=r),n.welcome&&(document.getElementById(`aa-prompt-welcome`).value=n.welcome),n.main_menu&&(document.getElementById(`aa-prompt-main-menu`).value=n.main_menu),n.invalid&&(document.getElementById(`aa-prompt-invalid`).value=n.invalid),n.timeout&&(document.getElementById(`aa-prompt-timeout`).value=n.timeout),n.transferring&&(document.getElementById(`aa-prompt-transferring`).value=n.transferring)}catch(e){console.error(`Error loading prompts:`,e)}}async function G(){try{let e=await fetch(`${API_BASE}/api/auto-attendant/menus/${q}/items`,{headers:pbxAuthHeaders()});if(e.status===404)return debugWarn(`Menu items endpoint returned 404 for menu '${q}', trying legacy API...`),await Wa();if(!e.ok){let t=await eo(e);throw Error(`Failed to load menu options: ${e.status} - ${t.error||e.statusText}`)}let t=await e.json(),n=document.getElementById(`aa-menu-options-table-body`);if(qa(),!t.items||t.items.length===0){n.innerHTML=`<tr><td colspan="5" class="no-data">No menu options configured. Click "Add Menu Option" to get started.</td></tr>`;return}let r=t.items.sort((e,t)=>e.digit===t.digit?0:e.digit===`*`?1:t.digit===`*`?-1:e.digit===`#`?1:t.digit===`#`?-1:e.digit.localeCompare(t.digit));n.innerHTML=``;for(let e of r){let t=document.createElement(`tr`),r=document.createElement(`td`);r.innerHTML=`<strong>${K(e.digit)}</strong>`,t.appendChild(r);let i=document.createElement(`td`);i.innerHTML=`${Ga(e.destination_type)} ${e.destination_type}`,t.appendChild(i);let a=document.createElement(`td`);e.destination_type===`submenu`?a.innerHTML=`<span style="color: #4CAF50; font-weight: bold;">${K(e.destination_value)}</span>`:a.textContent=e.destination_value,t.appendChild(a);let o=document.createElement(`td`);o.textContent=e.description,t.appendChild(o);let s=document.createElement(`td`);if(e.destination_type===`submenu`){let t=document.createElement(`button`);t.className=`btn btn-secondary`,t.textContent=`➡️ Open`,t.onclick=()=>Ka(e.destination_value),s.appendChild(t)}let c=document.createElement(`button`);c.className=`btn btn-primary`,c.textContent=`✏️ Edit`,c.onclick=()=>Xa(e.digit,e.destination_type,e.destination_value,e.description),s.appendChild(c);let l=document.createElement(`button`);l.className=`btn btn-danger`,l.textContent=`🗑️ Delete`,l.onclick=()=>Qa(e.digit),s.appendChild(l),t.appendChild(s),n.appendChild(t)}}catch(e){console.error(`Error loading menu options:`,e),showNotification(`Failed to load menu options`,`error`)}}async function Wa(){try{let e=await fetch(`${API_BASE}/api/auto-attendant/menu-options`,{headers:pbxAuthHeaders()});if(!e.ok)throw Error(`Failed to load menu options`);let t=await e.json(),n=document.getElementById(`aa-menu-options-table-body`);if(!t.menu_options||t.menu_options.length===0){n.innerHTML=`<tr><td colspan="5" class="no-data">No menu options configured.</td></tr>`;return}let r=t.menu_options.sort((e,t)=>e.digit===t.digit?0:e.digit===`*`?1:t.digit===`*`?-1:e.digit===`#`?1:t.digit===`#`?-1:e.digit.localeCompare(t.digit));n.innerHTML=``;for(let e of r){let t=document.createElement(`tr`),r=document.createElement(`td`);r.innerHTML=`<strong>${K(e.digit)}</strong>`,t.appendChild(r);let i=document.createElement(`td`);i.innerHTML=`📞 extension`,t.appendChild(i);let a=document.createElement(`td`);a.textContent=e.destination,t.appendChild(a);let o=document.createElement(`td`);o.textContent=e.description,t.appendChild(o);let s=document.createElement(`td`),c=document.createElement(`button`);c.className=`btn btn-primary`,c.textContent=`✏️ Edit`,c.onclick=()=>Xa(e.digit,`extension`,e.destination,e.description),s.appendChild(c);let l=document.createElement(`button`);l.className=`btn btn-danger`,l.textContent=`🗑️ Delete`,l.onclick=()=>Qa(e.digit),s.appendChild(l),t.appendChild(s),n.appendChild(t)}}catch(e){throw console.error(`Error loading legacy menu options:`,e),e}}function Ga(e){return{extension:`📞`,submenu:`📁`,queue:`👥`,voicemail:`📧`,operator:`🎧`}[e]??`❓`}async function Ka(e){q=e,await G()}async function qa(){try{let e=await fetch(`${API_BASE}/api/auto-attendant/menus/${q}`,{headers:pbxAuthHeaders()});if(e.ok){let t=await e.json(),n=document.getElementById(`breadcrumb-path`);if(n){let e=K(t.menu.menu_name);q!==`main`&&(e=`<button class="btn btn-secondary" onclick="navigateToMenu('main')" style="margin-right: 10px;">⬅️ Back to Main</button> ${e}`),n.innerHTML=e}}}catch(e){console.error(`Error updating breadcrumb:`,e)}}document.addEventListener(`DOMContentLoaded`,function(){let e=document.getElementById(`auto-attendant-config-form`);e&&e.addEventListener(`submit`,async function(e){e.preventDefault();let t={enabled:document.getElementById(`aa-enabled`).checked,extension:document.getElementById(`aa-extension`).value,timeout:parseInt(document.getElementById(`aa-timeout`).value,10),max_retries:parseInt(document.getElementById(`aa-max-retries`).value,10)};try{let e=await fetch(`${API_BASE}/api/auto-attendant/config`,{method:`PUT`,headers:pbxAuthHeaders(),body:JSON.stringify(t)});if(e.ok)showNotification(`Auto attendant configuration saved successfully`,`success`);else{let t=await e.json();showNotification(t.error||`Failed to save configuration`,`error`)}}catch(e){console.error(`Error saving auto attendant config:`,e),showNotification(`Failed to save configuration`,`error`)}})});function Ja(){document.getElementById(`add-menu-option-modal`).classList.add(`active`),document.getElementById(`add-menu-option-form`).reset()}function Ya(){document.getElementById(`add-menu-option-modal`).classList.remove(`active`)}function Xa(e,t,n,r){document.getElementById(`edit-menu-digit`).value=e,document.getElementById(`edit-menu-digit-display`).textContent=e,document.getElementById(`edit-menu-dest-type`).value=t??`extension`,document.getElementById(`edit-menu-description`).value=r,ro(`edit`),t===`submenu`?document.getElementById(`edit-menu-submenu`).value=n:document.getElementById(`edit-menu-destination`).value=n,document.getElementById(`edit-menu-option-modal`).classList.add(`active`)}function Za(){document.getElementById(`edit-menu-option-modal`).classList.remove(`active`)}async function Qa(e){if(confirm(`Are you sure you want to delete menu option for digit "${e}"?`))try{let t=await fetch(`${API_BASE}/api/auto-attendant/menus/${q}/items/${e}`,{method:`DELETE`,headers:pbxAuthHeaders()});if(t.ok)showNotification(`Menu option deleted successfully`,`success`),G();else{let e=await t.json();showNotification(e.error||`Failed to delete menu option`,`error`)}}catch(e){console.error(`Error deleting menu option:`,e),showNotification(`Failed to delete menu option`,`error`)}}document.addEventListener(`DOMContentLoaded`,function(){let e=document.getElementById(`add-menu-option-form`);e&&e.addEventListener(`submit`,async function(e){e.preventDefault();let t=document.getElementById(`new-menu-dest-type`).value,n;n=t===`submenu`?document.getElementById(`new-menu-submenu`).value:document.getElementById(`new-menu-destination`).value;let r={digit:document.getElementById(`new-menu-digit`).value,destination_type:t,destination_value:n,description:document.getElementById(`new-menu-description`).value};try{let e=await fetch(`${API_BASE}/api/auto-attendant/menus/${q}/items`,{method:`POST`,headers:pbxAuthHeaders(),body:JSON.stringify(r)});if(e.ok)showNotification(`Menu option added successfully`,`success`),Ya(),G();else{let t=await e.json();showNotification(t.error||`Failed to add menu option`,`error`)}}catch(e){console.error(`Error adding menu option:`,e),showNotification(`Failed to add menu option`,`error`)}})}),document.addEventListener(`DOMContentLoaded`,function(){let e=document.getElementById(`edit-menu-option-form`);e&&e.addEventListener(`submit`,async function(e){e.preventDefault();let t=document.getElementById(`edit-menu-digit`).value,n=document.getElementById(`edit-menu-dest-type`).value,r;r=n===`submenu`?document.getElementById(`edit-menu-submenu`).value:document.getElementById(`edit-menu-destination`).value;let i={destination_type:n,destination_value:r,description:document.getElementById(`edit-menu-description`).value};try{let e=await fetch(`${API_BASE}/api/auto-attendant/menus/${q}/items/${t}`,{method:`PUT`,headers:pbxAuthHeaders(),body:JSON.stringify(i)});if(e.ok)showNotification(`Menu option updated successfully`,`success`),Za(),G();else{let t=await e.json();showNotification(t.error||`Failed to update menu option`,`error`)}}catch(e){console.error(`Error updating menu option:`,e),showNotification(`Failed to update menu option`,`error`)}})}),document.addEventListener(`DOMContentLoaded`,function(){let e=document.getElementById(`auto-attendant-prompts-form`);e&&e.addEventListener(`submit`,async function(e){e.preventDefault();let t=document.getElementById(`voice-generation-status`),n=document.getElementById(`voice-generation-message`);t&&(t.style.display=`block`,n.textContent=`⏳ Saving prompts and regenerating voices using gTTS...`);let r={company_name:document.getElementById(`aa-company-name`).value,prompts:{welcome:document.getElementById(`aa-prompt-welcome`).value,main_menu:document.getElementById(`aa-prompt-main-menu`).value,invalid:document.getElementById(`aa-prompt-invalid`).value,timeout:document.getElementById(`aa-prompt-timeout`).value,transferring:document.getElementById(`aa-prompt-transferring`).value}};try{let e=await fetch(`${API_BASE}/api/auto-attendant/prompts`,{method:`PUT`,headers:pbxAuthHeaders(),body:JSON.stringify(r)});if(e.ok)showNotification(`Prompts saved and voices regenerated successfully!`,`success`),t&&(n.textContent=`✅ Voice prompts regenerated successfully using gTTS!`,setTimeout(()=>{t.style.display=`none`},3e3));else{let r=await e.json();showNotification(r.error||`Failed to save prompts`,`error`),t&&(n.textContent=`❌ Failed to regenerate voices`,setTimeout(()=>{t.style.display=`none`},3e3))}}catch(e){console.error(`Error saving prompts:`,e),showNotification(`Failed to save prompts`,`error`),t&&(n.textContent=`❌ Error: `+e.message,setTimeout(()=>{t.style.display=`none`},3e3))}})});function K(e){let t=document.createElement(`div`);return t.textContent=e,t.innerHTML}var q=`main`,$a=[];async function eo(e){try{return await e.json()}catch(e){let t=e?.message||String(e);return e instanceof SyntaxError||/JSON|Unexpected token/i.test(t)?{error:`Server returned an invalid response format`}:e instanceof TypeError||/network|NetworkError|fetch/i.test(t)?{error:`Network error while reading server response`}:(debugWarn(`Unexpected error parsing response:`,e),{error:`Unable to read server response`})}}async function to(){try{let e=await fetch(`${API_BASE}/api/auto-attendant/menus`,{headers:pbxAuthHeaders()});if(e.ok)return $a=(await e.json()).menus??[],debugLog(`Loaded ${$a.length} menu(s) for submenu selection`),$a;{let t=await eo(e);console.error(`Failed to load menus: ${e.status} ${e.statusText}`,t);let n=t.error||e.statusText;e.status===404?n+=`. API endpoint not found - check server version.`:e.status>=500&&(n+=`. Server error occurred.`),showNotification(`Failed to load menus: ${n}`,`error`)}}catch(e){console.error(`Error loading menus:`,e),showNotification(`Unable to connect to API: ${e.message}. Please check your connection.`,`error`)}return[]}async function no(){let e=await to(),t=document.getElementById(`new-menu-submenu`);if(t)if(t.innerHTML=`<option value="">Select a submenu</option>`,e.length===0){let e=document.createElement(`option`);e.value=``,e.textContent=`No menus available - create one first`,e.disabled=!0,t.appendChild(e),t.disabled=!0}else{for(let n of e)if(n.menu_id!==q){let e=document.createElement(`option`);e.value=n.menu_id,e.textContent=`${n.menu_name} (${n.menu_id})`,t.appendChild(e)}t.disabled=!1}let n=document.getElementById(`edit-menu-submenu`);if(n)if(n.innerHTML=`<option value="">Select a submenu</option>`,e.length===0){let e=document.createElement(`option`);e.value=``,e.textContent=`No menus available - create one first`,e.disabled=!0,n.appendChild(e),n.disabled=!0}else{for(let t of e)if(t.menu_id!==q){let e=document.createElement(`option`);e.value=t.menu_id,e.textContent=`${t.menu_name} (${t.menu_id})`,n.appendChild(e)}n.disabled=!1}let r=document.getElementById(`submenu-parent`);if(r)if(r.innerHTML=``,e.length===0){let e=document.createElement(`option`);e.value=``,e.textContent=`No parent menus available - check API connection or server status`,e.disabled=!0,e.selected=!0,r.appendChild(e),debugWarn(`No menus loaded for parent dropdown`)}else{for(let t of e){let e=document.createElement(`option`);e.value=t.menu_id,e.textContent=`${t.menu_name} (${t.menu_id})`,t.menu_id===`main`&&(e.selected=!0),r.appendChild(e)}debugLog(`Populated parent menu dropdown with ${e.length} menu(s)`)}}function ro(e){let t=document.getElementById(`${e}-menu-dest-type`),n=document.getElementById(`${e}-dest-extension-group`),r=document.getElementById(`${e}-dest-submenu-group`),i=document.getElementById(`${e}-menu-destination`),a=document.getElementById(`${e}-menu-submenu`),o=document.getElementById(`${e}-dest-label`),s=document.getElementById(`${e}-dest-help`);if(!t)return;let c=t.value;if(c===`submenu`)n&&(n.style.display=`none`),r&&(r.style.display=`block`),i&&(i.required=!1),a&&(a.required=!0),no();else if(n&&(n.style.display=`block`),r&&(r.style.display=`none`),i&&(i.required=!0),a&&(a.required=!1),o)switch(c){case`extension`:o.textContent=`Extension`,s&&(s.textContent=`Extension number to transfer to`);break;case`queue`:o.textContent=`Queue`,s&&(s.textContent=`Queue extension number`);break;case`voicemail`:o.textContent=`Voicemail Box`,s&&(s.textContent=`Voicemail box extension`);break;case`operator`:o.textContent=`Operator Extension`,s&&(s.textContent=`Operator extension number`);break}}function io(){no(),document.getElementById(`create-submenu-modal`).classList.add(`active`),document.getElementById(`create-submenu-form`).reset()}function ao(){document.getElementById(`create-submenu-modal`).classList.remove(`active`)}async function oo(){let e=document.getElementById(`menu-tree-container`);e.style.display===`none`?(e.style.display=`block`,await so()):e.style.display=`none`}async function so(){try{let e=await fetch(`${API_BASE}/api/auto-attendant/menu-tree`,{headers:pbxAuthHeaders()});if(!e.ok){let t=await eo(e);throw console.error(`Failed to load menu tree: ${e.status} ${e.statusText}`,t),e.status===404?Error(`Menu tree endpoint not found. The server may need to be restarted to load new API routes.`):e.status===500?Error(`Server error: ${t.error||`Internal server error`}`):Error(`Failed to load menu tree: ${t.error||e.statusText}`)}let t=await e.json(),n=document.getElementById(`menu-tree-view`);t.menu_tree?(n.innerHTML=co(t.menu_tree,0),debugLog(`Menu tree loaded successfully`)):(n.innerHTML=`<p style="color: #666;">No menu structure available</p>`,debugWarn(`Menu tree data is empty`))}catch(e){console.error(`Error loading menu tree:`,e),showNotification(`Failed to load menu tree: ${e.message}`,`error`);let t=document.getElementById(`menu-tree-view`);t&&(t.innerHTML=`<p class="error-message">
                <strong>Error:</strong> ${K(e.message)}<br>
                <small>Check the browser console for more details.</small>
            </p>`)}}function co(e,t){`  `.repeat(t);let n=`<div style="margin-left: ${t*20}px; margin-top: 5px;">`;if(n+=`<strong>${K(e.menu_name||e.menu_id)}</strong>`,e.items&&e.items.length>0)for(let r of e.items)n+=`<div style="margin-left: ${(t+1)*20}px; margin-top: 3px;">`,n+=`📌 ${K(r.digit)}: ${K(r.description??`No description`)} `,r.destination_type===`submenu`&&r.submenu?(n+=`<span style="color: #4CAF50;">[Submenu]</span>`,n+=`</div>`,n+=co(r.submenu,t+2)):(n+=`<span style="color: #666;">(${K(r.destination_type)}: ${K(r.destination_value)})</span>`,n+=`</div>`);return n+=`</div>`,n}document.addEventListener(`DOMContentLoaded`,function(){let e=document.getElementById(`create-submenu-form`);e&&e.addEventListener(`submit`,async function(e){e.preventDefault();let t={menu_id:document.getElementById(`submenu-id`).value,parent_menu_id:document.getElementById(`submenu-parent`).value,menu_name:document.getElementById(`submenu-name`).value,prompt_text:document.getElementById(`submenu-prompt`).value};try{let e=await fetch(`${API_BASE}/api/auto-attendant/menus`,{method:`POST`,headers:pbxAuthHeaders(),body:JSON.stringify(t)});if(e.ok)showNotification(`Submenu created successfully and voice generated!`,`success`),ao(),await to(),await G();else{let t=await e.json();showNotification(t.error||`Failed to create submenu`,`error`)}}catch(e){console.error(`Error creating submenu:`,e),showNotification(`Failed to create submenu`,`error`)}})}),window.loadAutoAttendantConfig=Ha,window.loadAutoAttendantPrompts=Ua,window.loadAutoAttendantMenuOptions=G,window.showAddMenuOptionModal=Ja,window.closeAddMenuOptionModal=Ya,window.editMenuOption=Xa,window.closeEditMenuOptionModal=Za,window.deleteMenuOption=Qa,window.navigateToMenu=Ka,window.toggleMenuTreeView=oo,window.showCreateSubmenuModal=io,window.closeCreateSubmenuModal=ao,window.updateDestinationFieldVisibility=ro;var lo=window.loadVoicemailForExtension;window.loadVoicemailForExtension=async function(){lo&&await lo();let e=document.getElementById(`vm-extension-select`).value;if(!e){document.getElementById(`voicemail-box-overview`).style.display=`none`;return}document.getElementById(`voicemail-box-overview`).style.display=`block`;try{let t=await fetch(`${API_BASE}/api/voicemail-boxes/${e}`,{headers:pbxAuthHeaders()});if(t.ok){let e=await t.json();document.getElementById(`vm-total-messages`).textContent=e.total_messages||0,document.getElementById(`vm-unread-messages`).textContent=e.unread_messages||0,document.getElementById(`vm-has-greeting`).textContent=e.has_custom_greeting?`Yes`:`No`}else console.error(`Failed to load mailbox details:`,t.status,t.statusText),document.getElementById(`vm-total-messages`).textContent=`0`,document.getElementById(`vm-unread-messages`).textContent=`0`,document.getElementById(`vm-has-greeting`).textContent=`Unknown`}catch(e){console.error(`Error loading mailbox details:`,e),document.getElementById(`vm-total-messages`).textContent=`0`,document.getElementById(`vm-unread-messages`).textContent=`0`,document.getElementById(`vm-has-greeting`).textContent=`Unknown`}},window.exportVoicemailBox=async function(){let e=document.getElementById(`vm-extension-select`).value;if(!e){showNotification(`Please select an extension first`,`error`);return}if(confirm(`Export all voicemails for extension ${e}?\n\nThis will download a ZIP file containing all voicemail messages and a manifest file.`))try{let t=await fetch(`${API_BASE}/api/voicemail-boxes/${e}/export`,{method:`POST`,headers:pbxAuthHeaders()});if(!t.ok){let e=await t.json();throw Error(e.error||`Failed to export voicemail box`)}let n=await t.blob(),r=t.headers.get(`Content-Disposition`),i=`voicemail_${e}_export.zip`;if(r){let e=r.match(/filename="?(.+)"?/i);e&&(i=e[1])}let a=window.URL.createObjectURL(n),o=document.createElement(`a`);o.href=a,o.download=i,document.body.appendChild(o),o.click(),window.URL.revokeObjectURL(a),document.body.removeChild(o),showNotification(`Voicemail box exported successfully`,`success`)}catch(e){console.error(`Error exporting voicemail box:`,e),showNotification(`Failed to export voicemail box: ${e.message}`,`error`)}},window.clearVoicemailBox=async function(){let e=document.getElementById(`vm-extension-select`).value;if(!e){showNotification(`Please select an extension first`,`error`);return}if(confirm(`⚠️ WARNING: Clear ALL voicemail messages for extension ${e}?\n\nThis action cannot be undone!\n\nConsider exporting the voicemail box first.`)&&confirm(`Are you absolutely sure? All voicemail messages will be permanently deleted.`))try{let t=await fetch(`${API_BASE}/api/voicemail-boxes/${e}/clear`,{method:`DELETE`,headers:pbxAuthHeaders()});if(t.ok){let e=await t.json();showNotification(e.message||`Voicemail box cleared successfully`,`success`),loadVoicemailForExtension()}else{let e=await t.json();showNotification(e.error||`Failed to clear voicemail box`,`error`)}}catch(e){console.error(`Error clearing voicemail box:`,e),showNotification(`Failed to clear voicemail box`,`error`)}},window.uploadCustomGreeting=async function(){let e=document.getElementById(`vm-extension-select`).value;if(!e){showNotification(`Please select an extension first`,`error`);return}let t=document.createElement(`input`);t.type=`file`,t.accept=`audio/wav`,t.onchange=async t=>{let n=t.target.files[0];if(n){if(!n.name.endsWith(`.wav`)){showNotification(`Please upload a WAV file`,`error`);return}try{let t=localStorage.getItem(`pbx_token`),r=t?{Authorization:`Bearer `+t}:{},i=await fetch(`${API_BASE}/api/voicemail-boxes/${e}/greeting`,{method:`PUT`,headers:r,body:await n.arrayBuffer()});if(i.ok)showNotification(`Custom greeting uploaded successfully`,`success`),loadVoicemailForExtension();else{let e=await i.json();showNotification(e.error||`Failed to upload greeting`,`error`)}}catch(e){console.error(`Error uploading greeting:`,e),showNotification(`Failed to upload greeting`,`error`)}}},t.click()},window.downloadCustomGreeting=async function(){let e=document.getElementById(`vm-extension-select`).value;if(!e){showNotification(`Please select an extension first`,`error`);return}try{let t=await fetch(`${API_BASE}/api/voicemail-boxes/${e}/greeting`,{headers:pbxAuthHeaders()});if(!t.ok)throw Error(`No custom greeting found`);let n=await t.blob(),r=window.URL.createObjectURL(n),i=document.createElement(`a`);i.href=r,i.download=`greeting_${e}.wav`,document.body.appendChild(i),i.click(),window.URL.revokeObjectURL(r),document.body.removeChild(i),showNotification(`Greeting downloaded`,`success`)}catch(e){console.error(`Error downloading greeting:`,e),showNotification(`No custom greeting found for this extension`,`error`)}},window.deleteCustomGreeting=async function(){let e=document.getElementById(`vm-extension-select`).value;if(!e){showNotification(`Please select an extension first`,`error`);return}if(confirm(`Delete custom greeting for extension ${e}?\n\nThe default system greeting will be used.`))try{let t=await fetch(`${API_BASE}/api/voicemail-boxes/${e}/greeting`,{method:`DELETE`,headers:pbxAuthHeaders()});if(t.ok)showNotification(`Custom greeting deleted successfully`,`success`),loadVoicemailForExtension();else{let e=await t.json();showNotification(e.error??`Failed to delete greeting`,`error`)}}catch(e){console.error(`Error deleting greeting:`,e),showNotification(`Failed to delete greeting`,`error`)}},window.loadAllVoicemailBoxes=async function(){try{let e=await fetch(`${API_BASE}/api/voicemail-boxes`,{headers:pbxAuthHeaders()});if(!e.ok)throw Error(`Failed to load voicemail boxes`);let t=await e.json();return debugLog(`All voicemail boxes:`,t.voicemail_boxes),t.voicemail_boxes}catch(e){return console.error(`Error loading voicemail boxes:`,e),[]}};function J(e){return String(e).replace(/[&<>"'\/]/g,function(e){switch(e){case`&`:return`&amp;`;case`<`:return`&lt;`;case`>`:return`&gt;`;case`"`:return`&quot;`;case`'`:return`&#39;`;case`/`:return`&#x2F;`;default:return e}})}var uo={jitsi:`Jitsi Meet`,matrix:`Matrix`,espocrm:`EspoCRM`};function Y(e,t=`info`,n=5e3){let r=document.getElementById(`quick-setup-notifications`);r||(r=document.createElement(`div`),r.id=`quick-setup-notifications`,r.style.cssText=`
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 10000;
            max-width: 400px;
        `,document.body.appendChild(r));let i=document.createElement(`div`),a={success:`#4CAF50`,error:`#f44336`,warning:`#ff9800`,info:`#2196F3`};if(i.style.cssText=`
        background-color: ${a[t]||a.info};
        color: white;
        padding: 16px 20px;
        margin-bottom: 10px;
        border-radius: 4px;
        box-shadow: 0 2px 5px rgba(0,0,0,0.2);
        display: flex;
        align-items: center;
        justify-content: space-between;
        animation: slideIn 0.3s ease-out;
    `,i.innerHTML=`
        <div style="flex: 1; padding-right: 10px;">${J(e)}</div>
        <button onclick="this.parentElement.remove()" style="
            background: none;
            border: none;
            color: white;
            font-size: 20px;
            cursor: pointer;
            padding: 0;
            width: 24px;
            height: 24px;
            display: flex;
            align-items: center;
            justify-content: center;
        ">×</button>
    `,!document.getElementById(`notification-animations`)){let e=document.createElement(`style`);e.id=`notification-animations`,e.textContent=`
            @keyframes slideIn {
                from {
                    transform: translateX(400px);
                    opacity: 0;
                }
                to {
                    transform: translateX(0);
                    opacity: 1;
                }
            }
        `,document.head.appendChild(e)}r.appendChild(i),n>0&&setTimeout(()=>{i.style.animation=`slideIn 0.3s ease-out reverse`,setTimeout(()=>i.remove(),300)},n)}async function fo(){go(),yo(),So(),await X()}async function X(){try{let e=(await(await fetch(`/api/config`,{headers:pbxAuthHeaders()})).json()).integrations||{},t=e.jitsi?.enabled||!1,n=document.getElementById(`quick-jitsi-enabled`),r=document.getElementById(`jitsi-status-badge`);n&&(n.checked=t),r&&(r.style.display=t?`inline-block`:`none`,r.style.backgroundColor=`#4CAF50`,r.style.color=`white`,r.textContent=`● Enabled`);let i=e.matrix?.enabled||!1,a=document.getElementById(`quick-matrix-enabled`),o=document.getElementById(`matrix-status-badge`);a&&(a.checked=i),o&&(o.style.display=i?`inline-block`:`none`,o.style.backgroundColor=`#9C27B0`,o.style.color=`white`,o.textContent=`● Enabled`);let s=e.espocrm?.enabled||!1,c=document.getElementById(`quick-espocrm-enabled`),l=document.getElementById(`espocrm-status-badge`);c&&(c.checked=s),l&&(l.style.display=s?`inline-block`:`none`,l.style.backgroundColor=`#2196F3`,l.style.color=`white`,l.textContent=`● Enabled`)}catch(e){console.error(`Failed to load integration status:`,e)}}async function po(e){document.getElementById(`quick-${e}-enabled`).checked?await mo(e):await ho(e)}async function mo(e){let t={jitsi:{enabled:!0,server_url:`https://localhost`,auto_create_rooms:!0,app_id:``,app_secret:``},matrix:{enabled:!0,homeserver_url:`https://localhost:8008`,bot_username:``,bot_password:"${MATRIX_BOT_PASSWORD}",notification_room:``,voicemail_room:``,missed_call_notifications:!0},espocrm:{enabled:!0,api_url:`https://localhost/api/v1`,api_key:"${ESPOCRM_API_KEY}",auto_create_contacts:!0,auto_log_calls:!0,screen_pop:!0}}[e];if(!t){Y(`Unknown integration: ${e}`,`error`);return}try{if((await fetch(`/api/config/section`,{method:`PUT`,headers:pbxAuthHeaders(),body:JSON.stringify({section:`integrations`,data:{[e]:t}})})).ok){let t=document.getElementById(`quick-${e}-enabled`);t&&(t.checked=!0),X();let n=`✅ ${uo[e]} enabled with default settings! The integration is now active.`;e===`matrix`?Y(`${n} Note: You need to set MATRIX_BOT_PASSWORD in your .env file for Matrix to work.`,`warning`,8e3):e===`espocrm`?Y(`${n} Note: You need to set ESPOCRM_API_KEY and api_url in the configuration tab.`,`warning`,8e3):Y(n,`success`),e===`jitsi`?go():e===`matrix`?yo():e===`espocrm`&&So()}else{Y(`Failed to enable ${uo[e]}`,`error`);let t=document.getElementById(`quick-${e}-enabled`);t&&(t.checked=!1)}}catch(t){Y(`Error enabling integration: ${t.message}`,`error`);let n=document.getElementById(`quick-${e}-enabled`);n&&(n.checked=!1)}}async function ho(e){try{let t=(await(await fetch(`/api/config`,{headers:pbxAuthHeaders()})).json()).integrations?.[e]??{};if(t.enabled=!1,(await fetch(`/api/config/section`,{method:`PUT`,headers:pbxAuthHeaders(),body:JSON.stringify({section:`integrations`,data:{[e]:t}})})).ok)X(),Y(`${uo[e]} has been disabled.`,`info`),e===`jitsi`?go():e===`matrix`?yo():e===`espocrm`&&So();else{Y(`Failed to disable ${uo[e]}`,`error`);let t=document.getElementById(`quick-${e}-enabled`);t&&(t.checked=!0)}}catch(t){Y(`Error disabling integration: ${t.message}`,`error`);let n=document.getElementById(`quick-${e}-enabled`);n&&(n.checked=!0)}}async function go(){try{let e=await fetch(`/api/config`,{headers:pbxAuthHeaders()});if(!e.ok){window.suppressErrorNotifications?debugLog(`Config endpoint returned error:`,e.status,`(feature may not be available or authentication required)`):console.error(`Failed to load Jitsi config:`,e.status);return}let t=(await e.json()).integrations?.jitsi??{};document.getElementById(`jitsi-enabled`).checked=t.enabled??!1,document.getElementById(`jitsi-server-url`).value=t.server_url??`https://localhost`,document.getElementById(`jitsi-auto-create-rooms`).checked=t.auto_create_rooms!==!1,document.getElementById(`jitsi-app-id`).value=t.app_id??``,document.getElementById(`jitsi-app-secret`).value=t.app_secret??``,_o()}catch(e){window.suppressErrorNotifications?debugLog(`Failed to load Jitsi config (expected if not authenticated):`,e.message):console.error(`Failed to load Jitsi config:`,e)}}function _o(){let e=document.getElementById(`jitsi-enabled`).checked;document.getElementById(`jitsi-settings`).style.display=e?`block`:`none`}document.getElementById(`jitsi-enabled`)?.addEventListener(`change`,_o),document.getElementById(`jitsi-config-form`)?.addEventListener(`submit`,async function(e){e.preventDefault();let t={enabled:document.getElementById(`jitsi-enabled`).checked,server_url:document.getElementById(`jitsi-server-url`).value,auto_create_rooms:document.getElementById(`jitsi-auto-create-rooms`).checked,app_id:document.getElementById(`jitsi-app-id`).value,app_secret:document.getElementById(`jitsi-app-secret`).value};try{(await fetch(`/api/config/section`,{method:`PUT`,headers:pbxAuthHeaders(),body:JSON.stringify({section:`integrations`,data:{jitsi:t}})})).ok?(Z(`Configuration saved successfully!`,`success`),X()):Z(`Failed to save configuration`,`error`)}catch(e){Z(`Error: ${J(e.message)}`,`error`)}});async function vo(){let e=document.getElementById(`jitsi-server-url`).value;Z(`Testing connection to ${J(e)}...`,`info`);try{let t=`${e}/external_api.js`;await fetch(t,{mode:`no-cors`});let n=`${e}/test-pbx-${Date.now()}`;Z(`✅ Connection successful!<br>Test meeting URL: <a href="${J(n)}" target="_blank">${J(n)}</a>`,`success`)}catch(e){Z(`⚠️ Could not verify connection. Server may still be accessible.<br>Error: ${J(e.message)}`,`warning`)}}function Z(e,t){let n=[`success`,`error`,`warning`,`info`].includes(t)?t:`info`,r=document.getElementById(`jitsi-status`);r.innerHTML=`<div class="alert alert-${n}">${e}</div>`}async function yo(){try{let e=await fetch(`/api/config`,{headers:pbxAuthHeaders()});if(!e.ok){window.suppressErrorNotifications?debugLog(`Config endpoint returned error:`,e.status,`(feature may not be available or authentication required)`):console.error(`Failed to load Matrix config:`,e.status);return}let t=(await e.json()).integrations?.matrix??{};document.getElementById(`matrix-enabled`).checked=t.enabled??!1,document.getElementById(`matrix-homeserver-url`).value=t.homeserver_url??`https://localhost:8008`,document.getElementById(`matrix-bot-username`).value=t.bot_username??``,document.getElementById(`matrix-bot-password`).value=t.bot_password??``,document.getElementById(`matrix-notification-room`).value=t.notification_room??``,document.getElementById(`matrix-voicemail-room`).value=t.voicemail_room??``,document.getElementById(`matrix-missed-call-notifications`).checked=t.missed_call_notifications!==!1,bo()}catch(e){window.suppressErrorNotifications?debugLog(`Failed to load Matrix config (expected if not authenticated):`,e.message):console.error(`Failed to load Matrix config:`,e)}}function bo(){let e=document.getElementById(`matrix-enabled`).checked;document.getElementById(`matrix-settings`).style.display=e?`block`:`none`}document.getElementById(`matrix-enabled`)?.addEventListener(`change`,bo),document.getElementById(`matrix-config-form`)?.addEventListener(`submit`,async function(e){e.preventDefault();let t={enabled:document.getElementById(`matrix-enabled`).checked,homeserver_url:document.getElementById(`matrix-homeserver-url`).value,bot_username:document.getElementById(`matrix-bot-username`).value,bot_password:document.getElementById(`matrix-bot-password`).value,notification_room:document.getElementById(`matrix-notification-room`).value,voicemail_room:document.getElementById(`matrix-voicemail-room`).value,missed_call_notifications:document.getElementById(`matrix-missed-call-notifications`).checked};try{(await fetch(`/api/config/section`,{method:`PUT`,headers:pbxAuthHeaders(),body:JSON.stringify({section:`integrations`,data:{matrix:t}})})).ok?(Q(`Configuration saved successfully!`,`success`),X()):Q(`Failed to save configuration`,`error`)}catch(e){Q(`Error: ${J(e.message)}`,`error`)}});async function xo(){let e=document.getElementById(`matrix-homeserver-url`).value,t=document.getElementById(`matrix-bot-username`).value,n=document.getElementById(`matrix-bot-password`).value;if(!t||!n){Q(`Please enter bot username and password`,`error`);return}Q(`Testing Matrix connection...`,`info`);try{let t=`${e}/_matrix/client/versions`,n=await fetch(t);if(!n.ok)throw Error(`Homeserver not accessible`);Q(`✅ Homeserver is accessible!<br>Supported versions: ${J((await n.json()).versions?.join(`, `)??`Unknown`)}<br><small>Note: Full authentication test requires server-side validation</small>`,`success`)}catch(e){Q(`❌ Connection failed: ${J(e.message)}`,`error`)}}function Q(e,t){let n=[`success`,`error`,`warning`,`info`].includes(t)?t:`info`,r=document.getElementById(`matrix-status`);r.innerHTML=`<div class="alert alert-${n}">${e}</div>`}async function So(){try{let e=await fetch(`/api/config`,{headers:pbxAuthHeaders()});if(!e.ok){window.suppressErrorNotifications?debugLog(`Config endpoint returned error:`,e.status,`(feature may not be available or authentication required)`):console.error(`Failed to load EspoCRM config:`,e.status);return}let t=(await e.json()).integrations?.espocrm??{};document.getElementById(`espocrm-enabled`).checked=t.enabled??!1,document.getElementById(`espocrm-api-url`).value=t.api_url??`https://localhost/api/v1`,document.getElementById(`espocrm-api-key`).value=t.api_key??``,document.getElementById(`espocrm-auto-create-contacts`).checked=t.auto_create_contacts!==!1,document.getElementById(`espocrm-auto-log-calls`).checked=t.auto_log_calls!==!1,document.getElementById(`espocrm-screen-pop`).checked=t.screen_pop!==!1,Co()}catch(e){window.suppressErrorNotifications?debugLog(`Failed to load EspoCRM config (expected if not authenticated):`,e.message):console.error(`Failed to load EspoCRM config:`,e)}}function Co(){let e=document.getElementById(`espocrm-enabled`).checked;document.getElementById(`espocrm-settings`).style.display=e?`block`:`none`}document.getElementById(`espocrm-enabled`)?.addEventListener(`change`,Co),document.getElementById(`espocrm-config-form`)?.addEventListener(`submit`,async function(e){e.preventDefault();let t={enabled:document.getElementById(`espocrm-enabled`).checked,api_url:document.getElementById(`espocrm-api-url`).value,api_key:document.getElementById(`espocrm-api-key`).value,auto_create_contacts:document.getElementById(`espocrm-auto-create-contacts`).checked,auto_log_calls:document.getElementById(`espocrm-auto-log-calls`).checked,screen_pop:document.getElementById(`espocrm-screen-pop`).checked};try{(await fetch(`/api/config/section`,{method:`PUT`,headers:pbxAuthHeaders(),body:JSON.stringify({section:`integrations`,data:{espocrm:t}})})).ok?($(`Configuration saved successfully!`,`success`),X()):$(`Failed to save configuration`,`error`)}catch(e){$(`Error: ${J(e.message)}`,`error`)}});async function wo(){let e=document.getElementById(`espocrm-api-url`).value,t=document.getElementById(`espocrm-api-key`).value;if(!e||!t){$(`Please enter API URL and API Key`,`error`);return}$(`Testing EspoCRM connection...`,`info`);try{let n=`${e}/App/user`,r=await fetch(n,{headers:{"X-Api-Key":t,"Content-Type":`application/json`}});if(r.ok)$(`✅ Connection successful!<br>Connected as: ${J((await r.json()).userName??`Unknown`)}<br>EspoCRM is ready for integration.`,`success`);else throw Error(`API returned status ${r.status}`)}catch(e){$(`❌ Connection failed: ${J(e.message)}<br>Check API URL and API Key.`,`error`)}}function $(e,t){let n=[`success`,`error`,`warning`,`info`].includes(t)?t:`info`,r=document.getElementById(`espocrm-status`);r.innerHTML=`<div class="alert alert-${n}">${e}</div>`}async function To(){let e=document.getElementById(`jitsi-instant-room`).value??``;try{let t=await fetch(`/api/integrations/jitsi/instant`,{method:`POST`,headers:pbxAuthHeaders(),body:JSON.stringify({room_name:e,extension:`admin`})});if(!t.ok){let e=await t.json();throw Error(e.error||`Failed to create meeting`)}Do((await t.json()).meeting_url),Y(`Meeting created successfully!`,`success`)}catch(e){Y(`Failed to create meeting: ${e.message}`,`error`)}}async function Eo(){let e=document.getElementById(`jitsi-schedule-subject`).value,t=document.getElementById(`jitsi-schedule-duration`).value;if(!e){Y(`Please enter a meeting subject`,`warning`);return}try{let n=await fetch(`/api/integrations/jitsi/meetings`,{method:`POST`,headers:pbxAuthHeaders(),body:JSON.stringify({subject:e,duration:parseInt(t),moderator_name:`Admin`})});if(!n.ok){let e=await n.json();throw Error(e.error||`Failed to schedule meeting`)}Do((await n.json()).meeting_url),Y(`Meeting scheduled successfully!`,`success`)}catch(e){Y(`Failed to schedule meeting: ${e.message}`,`error`)}}function Do(e){let t=document.getElementById(`jitsi-meeting-result`),n=document.getElementById(`jitsi-meeting-url`);n.value=e,t.style.display=`block`}async function Oo(){let e=document.getElementById(`jitsi-meeting-url`),t=e.value;try{navigator.clipboard&&navigator.clipboard.writeText?(await navigator.clipboard.writeText(t),Y(`Meeting URL copied to clipboard!`,`success`,3e3)):(e.select(),document.execCommand(`copy`),Y(`Meeting URL copied to clipboard!`,`success`,3e3))}catch{e.select(),document.execCommand(`copy`),Y(`Meeting URL copied to clipboard!`,`success`,3e3)}}function ko(){let e=document.getElementById(`jitsi-meeting-url`).value;e&&window.open(e,`_blank`)}document.addEventListener(`DOMContentLoaded`,function(){let e=document.getElementById(`matrix-room-select`);e&&e.addEventListener(`change`,function(){let e=document.getElementById(`matrix-custom-room`);this.value===`custom`?e.style.display=`block`:e.style.display=`none`})});async function Ao(){let e=document.getElementById(`matrix-room-select`).value,t=document.getElementById(`matrix-custom-room-id`).value,n=document.getElementById(`matrix-message-text`).value;if(!n){Y(`Please enter a message`,`warning`);return}let r=null;if(e===`custom`){if(r=t,!r){Y(`Please enter a custom room ID`,`warning`);return}}else(e===`notification`||e===`voicemail`)&&(r=null);try{let e=await fetch(`/api/integrations/matrix/messages`,{method:`POST`,headers:pbxAuthHeaders(),body:JSON.stringify({room_id:r,message:n,msg_type:`m.text`})});if(!e.ok){let t=await e.json();throw Error(t.error||`Failed to send message`)}await e.json(),No(`✅ Message sent successfully!`,`success`),document.getElementById(`matrix-message-text`).value=``}catch(e){No(`❌ Failed to send message: ${e.message}`,`error`)}}async function jo(){try{let e=await fetch(`/api/integrations/matrix/notifications`,{method:`POST`,headers:pbxAuthHeaders(),body:JSON.stringify({message:`🧪 Test notification from PBX Admin Panel - ${new Date().toLocaleString()}`})});if(!e.ok){let t=await e.json();throw Error(t.error||`Failed to send notification`)}No(`✅ Test notification sent successfully!`,`success`)}catch(e){No(`❌ Failed to send notification: ${e.message}`,`error`)}}async function Mo(){let e=document.getElementById(`matrix-new-room-name`).value,t=document.getElementById(`matrix-new-room-topic`).value;if(!e){Y(`Please enter a room name`,`warning`);return}try{let n=await fetch(`/api/integrations/matrix/rooms`,{method:`POST`,headers:pbxAuthHeaders(),body:JSON.stringify({name:e,topic:t})});if(!n.ok){let e=await n.json();throw Error(e.error||`Failed to create room`)}Po((await n.json()).room_id),Y(`Room created successfully!`,`success`),document.getElementById(`matrix-new-room-name`).value=``,document.getElementById(`matrix-new-room-topic`).value=``}catch(e){Y(`Failed to create room: ${e.message}`,`error`)}}function No(e,t){let n=document.getElementById(`matrix-message-result`),r=document.getElementById(`matrix-message-result-text`);r.textContent=e,n.style.display=`block`,n.querySelector(`.info-box`).style.backgroundColor=t===`success`?`#e8f5e9`:`#ffebee`,setTimeout(()=>{n.style.display=`none`},5e3)}function Po(e){let t=document.getElementById(`matrix-room-result`),n=document.getElementById(`matrix-new-room-id`);n.textContent=e,t.style.display=`block`}async function Fo(){let e=document.getElementById(`espocrm-search-type`).value,t=document.getElementById(`espocrm-search-term`).value;if(!t){Y(`Please enter a search term`,`warning`);return}try{let n=await fetch(`/api/integrations/espocrm/contacts/search?${e===`phone`?`phone`:e===`email`?`email`:`name`}=${encodeURIComponent(t)}`,{method:`GET`,headers:pbxAuthHeaders()});if(!n.ok){let e=await n.json();throw Error(e.error||`Failed to search contact`)}Io(await n.json())}catch(e){Y(`Failed to search contact: ${e.message}`,`error`)}}function Io(e){let t=document.getElementById(`espocrm-search-results`),n=document.getElementById(`espocrm-contact-details`);if(!e.success||!e.contact){n.innerHTML=`<div class="info-box" style="background-color: #fff3e0;">No contact found</div>`,t.style.display=`block`;return}let r=e.contact,i=`<div class="info-box" style="background-color: #e8f5e9;">`;i+=`<h4>✅ Contact Found</h4>`,i+=`<table style="width: 100%; margin-top: 10px;">`,r.name&&(i+=`<tr><td><strong>Name:</strong></td><td>${J(r.name)}</td></tr>`),r.email&&(i+=`<tr><td><strong>Email:</strong></td><td>${J(r.email)}</td></tr>`),r.phone&&(i+=`<tr><td><strong>Phone:</strong></td><td>${J(r.phone)}</td></tr>`),r.company&&(i+=`<tr><td><strong>Company:</strong></td><td>${J(r.company)}</td></tr>`),r.title&&(i+=`<tr><td><strong>Title:</strong></td><td>${J(r.title)}</td></tr>`),r.id&&(i+=`<tr><td><strong>CRM ID:</strong></td><td>${J(r.id)}</td></tr>`),i+=`</table></div>`,n.innerHTML=i,t.style.display=`block`}async function Lo(){let e=document.getElementById(`espocrm-new-firstname`).value,t=document.getElementById(`espocrm-new-lastname`).value,n=document.getElementById(`espocrm-new-phone`).value,r=document.getElementById(`espocrm-new-email`).value,i=document.getElementById(`espocrm-new-company`).value,a=document.getElementById(`espocrm-new-title`).value;if(!e||!t){Y(`Please enter first and last name`,`warning`);return}if(!n&&!r){Y(`Please enter at least phone or email`,`warning`);return}try{let o=await fetch(`/api/integrations/espocrm/contacts`,{method:`POST`,headers:pbxAuthHeaders(),body:JSON.stringify({name:`${e} ${t}`,phone:n,email:r,company:i,title:a})});if(!o.ok){let e=await o.json();throw Error(e.error||`Failed to create contact`)}Ro(`✅ Contact created successfully! CRM ID: ${(await o.json()).contact?.id??`unknown`}`),document.getElementById(`espocrm-new-firstname`).value=``,document.getElementById(`espocrm-new-lastname`).value=``,document.getElementById(`espocrm-new-phone`).value=``,document.getElementById(`espocrm-new-email`).value=``,document.getElementById(`espocrm-new-company`).value=``,document.getElementById(`espocrm-new-title`).value=``}catch(e){Ro(`❌ Failed to create contact: ${e.message}`)}}function Ro(e){let t=document.getElementById(`espocrm-create-result`),n=document.getElementById(`espocrm-create-result-text`);n.textContent=e,t.style.display=`block`,setTimeout(()=>{t.style.display=`none`},5e3)}window.loadOpenSourceIntegrations=fo,window.loadJitsiConfig=go,window.loadMatrixConfig=yo,window.loadEspoCRMConfig=So,window.toggleJitsiSettings=_o,window.testJitsiConnection=vo,window.toggleMatrixSettings=bo,window.testMatrixConnection=xo,window.toggleEspoCRMSettings=Co,window.testEspoCRMConnection=wo,window.quickToggleIntegration=po,window.quickSetupIntegration=mo,window.disableIntegration=ho,window.createInstantJitsiMeeting=To,window.scheduleJitsiMeeting=Eo,window.copyJitsiMeetingUrl=Oo,window.openJitsiMeeting=ko,window.sendMatrixMessage=Ao,window.sendMatrixTestNotification=jo,window.createMatrixRoom=Mo,window.searchEspoCRMContact=Fo,window.createEspoCRMContact=Lo,setTimeout(function(){if(!window.currentUser&&!document.querySelector(`.tab-content.active`)){console.warn(`Page may not have loaded correctly. Checking for common issues...`);var e=document.querySelector(`.sidebar`);if(e){var t=window.getComputedStyle(e);if(t.width===`auto`||t.width===`0px`){console.error(`CSS may not be loaded correctly. Try clearing your browser cache:`),console.error(`  - Press Ctrl+Shift+R (Windows/Linux) or Cmd+Shift+R (Mac)`),console.error(`  - See BROWSER_CACHE_FIX.md for detailed instructions`);var n=document.createElement(`div`);n.className=`page-load-alert`,n.innerHTML=`<strong class="page-load-alert-title">⚠️ Page Loading Issue</strong><p>The admin panel may not be displaying correctly due to cached files.</p><p class="page-load-alert-action">Press <code>Ctrl+Shift+R</code> (or <code>Cmd+Shift+R</code> on Mac) to reload without cache</p>`;var r=document.createElement(`button`);r.className=`page-load-alert-dismiss`,r.textContent=`Dismiss`,r.addEventListener(`click`,function(){n.remove()}),n.appendChild(r),document.body.appendChild(n)}}}else console.log(`Page loaded successfully at`,new Date().toISOString())},3e3),(function(){var e=document.getElementById(`sidebar-toggle`),t=document.querySelector(`.sidebar`),n=document.getElementById(`sidebar-overlay`);if(!e||!t||!n)return;function r(){t.classList.add(`open`),n.classList.add(`active`),e.classList.add(`active`),e.setAttribute(`aria-expanded`,`true`)}function i(){t.classList.remove(`open`),n.classList.remove(`active`),e.classList.remove(`active`),e.setAttribute(`aria-expanded`,`false`)}e.addEventListener(`click`,function(){t.classList.contains(`open`)?i():r()}),n.addEventListener(`click`,i),t.addEventListener(`click`,function(e){e.target.closest(`.tab-button`)&&i()}),document.addEventListener(`keydown`,function(e){e.key===`Escape`&&t.classList.contains(`open`)&&i()})})();