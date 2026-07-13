// Aguarda o carregamento completo do DOM (Document Object Model)
document.addEventListener("DOMContentLoaded", function() {
    
    // Auto-fechar os alertas (Flash Messages) após 4 segundos
    setTimeout(function() {
        let alerts = document.querySelectorAll('.alert');
        alerts.forEach(function(alert) {
            let bsAlert = new bootstrap.Alert(alert);
            bsAlert.close();
        });
    }, 4000);

    // Validação opcional no lado do cliente para tamanho de ficheiro antes do upload
    const uploadForm = document.querySelector('form[action="/upload"]');
    if (uploadForm) {
        const fileInput = uploadForm.querySelector('input[type="file"]');
        uploadForm.addEventListener('submit', function(e) {
            if (fileInput.files.length > 0) {
                const fileSize = fileInput.files[0].size / 1024 / 1024; // Converte para MB
                const maxLimit = 32; // Limite de 32MB definido no app.py
                
                if (fileSize > maxLimit) {
                    alert(`O ficheiro excede o limite permitido de ${maxLimit}MB.`);
                    e.preventDefault(); // Cancela o envio
                }
            }
        });
    }
});

// PREVIEW DA IMAGEM DE PERFIL
const input = document.querySelector(
    'input[name="profile_image"]'
);

if (input) {

    input.addEventListener("change", function () {

        const file = this.files[0];

        if (!file)
            return;

        const reader = new FileReader();

        reader.onload = function (e) {

            document
                .getElementById("preview")
                .src = e.target.result;

        }

        reader.readAsDataURL(file);

    });

}