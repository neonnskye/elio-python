const btn = document.getElementById('enroll-btn');
        const input = document.getElementById('name-input');
        const status = document.getElementById('status-msg');

        async function enroll() {
            const name = input.value.trim();
            if (!name) {
                showStatus('Enter a name first.', false);
                return;
            }

            btn.disabled = true;
            btn.textContent = 'Enrolling…';
            status.textContent = '';

            try {
                const res = await fetch('/enroll', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name })
                });
                const data = await res.json();
                showStatus(data.message, data.ok);
                if (data.ok) input.value = '';
            } catch (err) {
                showStatus('Request failed.', false);
            } finally {
                btn.disabled = false;
                btn.textContent = 'Enroll';
            }
        }

        function showStatus(msg, ok) {
            status.textContent = msg;
            status.className = ok ? 'ok' : 'err';
        }

        btn.addEventListener('click', enroll);
        input.addEventListener('keydown', e => { if (e.key === 'Enter') enroll(); });