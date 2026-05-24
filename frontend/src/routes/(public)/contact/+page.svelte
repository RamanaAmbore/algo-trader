<script>
  import { submitContact } from '$lib/api';

  let form = { name: '', email: '', message: '' };
  let submitting = false;
  let success    = '';
  let error      = '';

  async function submit() {
    submitting = true;
    success    = '';
    error      = '';
    try {
      const d = await submitContact(form);
      success = d.detail || 'Your message has been sent. We will get back to you shortly.';
      form = { name: '', email: '', message: '' };
    } catch (e) {
      error = /** @type {Error} */ (e).message || 'Failed to send message.';
    } finally {
      submitting = false;
    }
  }
</script>

<svelte:head>
  <title>Contact Us | RamboQuant Analytics</title>
  <meta name="description" content="Get in touch with RamboQuant Analytics LLP — for partnership inquiries, support, or general questions." />

  <!-- Open Graph -->
  <meta property="og:title" content="Contact Us | RamboQuant Analytics" />
  <meta property="og:description" content="Get in touch with RamboQuant Analytics LLP — for partnership inquiries, support, or general questions." />
  <meta property="og:url" content="https://ramboq.com/contact" />
  <meta property="og:type" content="website" />
  <meta property="og:image" content="https://ramboq.com/og-image-thumb.png?v=2" />
  <meta property="og:image:width" content="600" />
  <meta property="og:image:height" content="600" />
  <meta property="og:image:alt" content="RamboQuant Analytics brand mark — teal bull inside a champagne-gold ring on a dark teal background." />
  <meta property="og:site_name" content="RamboQuant Analytics" />

  <!-- Twitter -->
  <meta name="twitter:card" content="summary" />
  <meta name="twitter:title" content="Contact Us | RamboQuant Analytics" />
  <meta name="twitter:description" content="Get in touch with RamboQuant Analytics LLP — for partnership inquiries, support, or general questions." />
  <meta name="twitter:image" content="https://ramboq.com/og-image-thumb.png?v=2" />
  <meta name="twitter:image:alt" content="RamboQuant Analytics brand mark — teal bull inside a champagne-gold ring on a dark teal background." />
</svelte:head>

<!-- Wider on laptop so the form doesn't float in a sea of cream;
     mobile keeps the existing tight width via the breakpoint cap. -->
<div class="contact-wrap mx-auto">
  <div class="pub-card rounded-lg shadow-sm p-5 pt-4">
  <h1 class="page-heading">Contact</h1>
  {#if success}
    <div class="pub-banner-success mb-4 p-3 rounded text-sm">{success}</div>
  {/if}
  {#if error}
    <div class="pub-banner-error mb-4 p-3 rounded text-sm">{error}</div>
  {/if}

  <div class="space-y-4">
    <div>
      <label class="field-label" for="c-name">Name</label>
      <input id="c-name" bind:value={form.name} class="field-input" placeholder="Your name" />
    </div>
    <div>
      <label class="field-label" for="c-email">Email</label>
      <input id="c-email" type="email" bind:value={form.email} class="field-input" placeholder="you@example.com" />
    </div>
    <div>
      <label class="field-label" for="c-msg">Message</label>
      <textarea
        id="c-msg"
        bind:value={form.message}
        class="field-input min-h-[120px] resize-y"
        placeholder="How can we help you?"
      ></textarea>
    </div>
    <button
      onclick={submit}
      disabled={submitting || !form.name || !form.email || !form.message}
      class="contact-send-btn w-full disabled:opacity-50 disabled:cursor-not-allowed"
    >
      {submitting ? 'Sending…' : 'Send Message'}
    </button>
  </div>
  </div>
</div>

<style>
  /* Contact card width — laptop gets a comfortable medium width so it
     doesn't float in cream void. Mobile caps at the viewport. */
  .contact-wrap {
    max-width: 32rem;
  }
  @media (max-width: 480px) {
    .contact-wrap { max-width: 100%; }
  }

  /* Send Message — solid champagne CTA, matches the home page's
     primary buttons. Beige .btn-primary on a cream card read as
     near-disabled even when enabled. */
  .contact-send-btn {
    display: inline-block;
    font-size: 0.9rem;
    font-weight: 700;
    padding: 0.55rem 1.1rem;
    border-radius: 0.375rem;
    background: #d4920c;
    color: #fff;
    border: 1px solid #d4920c;
    cursor: pointer;
    transition: background 0.12s, border-color 0.12s;
    letter-spacing: 0.01em;
  }
  .contact-send-btn:hover:not(:disabled) {
    background: #b87a0a;
    border-color: #b87a0a;
  }
  .contact-send-btn:disabled {
    background: #d4c898;
    border-color: #b8a870;
    color: #5a4010;
  }
</style>
