    marked.setOptions({ gfm: true, breaks: true });
    let sessionId = localStorage.getItem('luintix_session_id');

    function toggleModal(show) {
        document.getElementById('sampleModal').style.display = show ? 'flex' : 'none';
    }

    function selectSample(text) {
        document.getElementById('userInput').value = text;
        toggleModal(false);
        document.getElementById('chatForm').dispatchEvent(new Event('submit'));
    }

    document.getElementById('chatForm').addEventListener('submit', async function(e) {
        e.preventDefault();
        
        const inputEl = document.getElementById('userInput');
        const chatBox = document.getElementById('chatBox');
        const text = inputEl.value.trim();
        
        if (!text) return;

        const userDiv = document.createElement('div');
        userDiv.className = 'message user-msg';
        userDiv.textContent = text;
        chatBox.appendChild(userDiv);
        
        inputEl.value = '';
        chatBox.scrollTop = chatBox.scrollHeight;

        const loadingDiv = document.createElement('div');
        loadingDiv.className = 'message agent-msg';
        loadingDiv.textContent = 'Luintix is checking...';
        chatBox.appendChild(loadingDiv);
        chatBox.scrollTop = chatBox.scrollHeight;

        try {
            const tenantId = document.getElementById('tenantSelect')?.value || 'COMP-SHOPIFY';
            const res = await fetch('/api/chat', {
                method: 'POST',
                headers: { 
                    'Content-Type': 'application/json',
                    'X-Company-ID': tenantId
                },
                body: JSON.stringify({ user_input: text, session_id: sessionId })
            });

            if (!res.ok) {
                const errBody = await res.json().catch(() => ({}));
                throw new Error(errBody.detail || `Server returned error: ${res.status}`);
            }

            const data = await res.json();
            
            if (data.session_id) {
                sessionId = data.session_id;
                localStorage.setItem('luintix_session_id', sessionId);
            }

            let parsedHtml = marked.parse(data.agent_response || 'No response returned.');
            
            const tempDiv = document.createElement('div');
            tempDiv.innerHTML = parsedHtml;

            const tables = tempDiv.querySelectorAll('table');
            tables.forEach(table => {
                if (!table.parentElement.classList.contains('table-wrapper')) {
                    const wrapper = document.createElement('div');
                    wrapper.className = 'table-wrapper';
                    table.parentNode.insertBefore(wrapper, table);
                    wrapper.appendChild(table);
                }
            });

            loadingDiv.innerHTML = tempDiv.innerHTML;
        } catch (err) {
            loadingDiv.textContent = `Error: ${err.message || 'Could not connect to backend server.'}`;
        }
        
        chatBox.scrollTop = chatBox.scrollHeight;
    });