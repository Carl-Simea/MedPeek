document.addEventListener("DOMContentLoaded", () => {
    // 1. Theme Toggle
    const htmlElement = document.documentElement;
    const themeToggleBtn = document.getElementById('theme-toggle');
    
    if (themeToggleBtn) {
        themeToggleBtn.addEventListener('click', () => {
            const currentTheme = htmlElement.getAttribute('data-bs-theme');
            const newTheme = currentTheme === 'light' ? 'dark' : 'light';
            
            htmlElement.setAttribute('data-bs-theme', newTheme);
            localStorage.setItem('theme', newTheme);
        });
    }

    // 2. Chat Auto-Scroll
    const chatWindow = document.getElementById('chat-window');
    if (chatWindow) {
        chatWindow.scrollTop = chatWindow.scrollHeight;
    }

    // 3. File Selection Indicator
    const fileInput = document.getElementById('xray_upload');
    const fileText = document.getElementById('file-selection-text');
    if (fileInput && fileText) {
        fileInput.addEventListener('change', function() {
            if (this.files && this.files.length > 0) {
                fileText.textContent = `📎 ${this.files.length} image(s) ready to send`;
                fileText.classList.remove('d-none');
            } else {
                fileText.textContent = '';
                fileText.classList.add('d-none');
            }
        });
    }

    // 4. Submit Form on Enter (Textarea)
    const chatInput = document.querySelector('.chat-input');
    const chatForm = chatInput ? chatInput.closest('form') : null;
    
    if (chatInput && chatForm) {
        chatInput.addEventListener('keydown', function(e) {
            // Submit if Enter is pressed without Shift
            if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault(); // Prevent new line
                
                const hasText = chatInput.value.trim() !== '';
                const hasFile = fileInput && fileInput.files.length > 0;
                
                // Only submit if there is actually text or a file
                if (hasText || hasFile) {
                    chatForm.submit();
                }
            }
        });
    }
});