document.addEventListener("DOMContentLoaded", () => {
    // 1. Theme Toggle Logic
    const themeToggleBtn = document.getElementById('theme-toggle');
    const htmlElement = document.documentElement;
    
    if (themeToggleBtn) {
        themeToggleBtn.addEventListener('click', () => {
            const currentTheme = htmlElement.getAttribute('data-bs-theme');
            const newTheme = currentTheme === 'light' ? 'dark' : 'light';
            htmlElement.setAttribute('data-bs-theme', newTheme);
            localStorage.setItem('theme', newTheme);
        });
    }

    // 2. Chat Scroll to Bottom
    const chatWindow = document.getElementById('chat-window');
    if (chatWindow) {
        chatWindow.scrollTop = chatWindow.scrollHeight;
    }

    // 3. File Input UI Update
    const fileInput = document.getElementById('xray_upload');
    const fileText = document.getElementById('file-selection-text');
    
    if (fileInput && fileText) {
        fileInput.addEventListener('change', function() {
            if (this.files && this.files.length > 0) {
                // Map the file names and join them with a comma
                let fileNames = Array.from(this.files).map(f => f.name).join(', ');
                fileText.textContent = `Selected: ${fileNames}`;
                fileText.classList.remove('d-none');
            } else {
                fileText.classList.add('d-none');
            }
        });
    }
});

// 4. Sample Prompts Injector (Global scope for inline onclick buttons)
window.setPrompt = function(text) {
    const input = document.querySelector('.chat-input');
    if (input) {
        input.value = text;
        input.focus();
        // Trigger the auto-resize behavior
        input.style.height = 'auto';
        input.style.height = input.scrollHeight + 'px';
    }
};