// Global utilities for ProDough Forecasting

// Auto-dismiss alerts after 5 seconds
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.alert-dismissible').forEach(function(alert) {
    setTimeout(function() {
      var bsAlert = bootstrap.Alert.getOrCreateInstance(alert);
      if (bsAlert) bsAlert.close();
    }, 5000);
  });
});
