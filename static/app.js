(function () {
  var t; try { t = localStorage.getItem('theme'); } catch (e) {}
  if (!t) t = matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
  document.documentElement.dataset.theme = t;
  document.addEventListener('DOMContentLoaded', function () {
    document.getElementById('theme').onclick = function () {
      var n = document.documentElement.dataset.theme === 'dark' ? 'light' : 'dark';
      document.documentElement.dataset.theme = n; try { localStorage.setItem('theme', n); } catch (e) {}
    };
    document.getElementById('menu').onclick = function () { document.getElementById('links').classList.toggle('open'); };
    var method = document.getElementById('delivery-method');
    if (method) {
      var fields = document.getElementById('delivery-fields');
      var deliveryInputs = fields.querySelectorAll('input,select');
      var updateDeliveryFields = function () {
        var pickup = method.value === 'pickup';
        fields.hidden = pickup;
        deliveryInputs.forEach(function (input) {
          input.required = !pickup && (input.name !== 'landmark' && input.name !== 'map_pin');
          input.disabled = pickup;
        });
      };
      method.addEventListener('change', updateDeliveryFields);
      updateDeliveryFields();
    }
    var io = new IntersectionObserver(function (es) { es.forEach(function (e) { if (e.isIntersecting) { e.target.classList.add('in'); io.unobserve(e.target); } }); }, { threshold: .12 });
    document.querySelectorAll('.reveal').forEach(function (el) { io.observe(el); });
  });
})();
document.addEventListener('DOMContentLoaded', function () { var p = document.getElementById('print'); if (p) p.onclick = function () { print(); }; });
