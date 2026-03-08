#!/usr/bin/env python3
# =============================================================================
# SMTP Email Sender Module
# =============================================================================
# Handles email composition and sending via SMTP for the GrafMail-Grafana Screenshot
# Tool. Sends HTML-formatted emails with screenshots embedded inline via
# Content-ID, plus a plain-text fallback.
#
# Supports:
#   - TLS + Auth (STARTTLS on port 587)
#   - SSL (SMTP_SSL on port 465)
#   - No-TLS No-Auth (internal relay on port 25)
#   - CC / BCC recipients
#   - Inline embedded images (Content-ID)
#   - Custom email body (EMAIL_BODY_MESSAGE)
#   - No-images notification mode
#
# Author: Md Mahin Rahman
# =============================================================================

import ssl
import html as html_module
import smtplib
import logging
import mimetypes
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path
from typing import List, Tuple, Optional

logger = logging.getLogger(__name__)

# Maximum total attachment size for email (10 MB)
MAX_ATTACHMENT_SIZE_MB = 10
MAX_ATTACHMENT_SIZE_BYTES = MAX_ATTACHMENT_SIZE_MB * 1024 * 1024


class SmtpSender:
    """Handles email composition and sending via SMTP (SMTP_INTERNAL mode).

    Sends HTML emails with inline-embedded screenshots. Falls back to
    plain-text for clients that don't render HTML.
    """

    def __init__(self, config):
        """Initialize SMTP sender from Config object."""
        self.config = config
        self.host = config.get('SMTP_HOST')
        self.port = config.get_int('SMTP_PORT', 587)
        self.user = config.get('SMTP_USER')
        self.password = config.get('SMTP_PASSWORD')
        self.from_addr = config.get('SMTP_FROM')
        self.to_addrs = config.get_list('SMTP_TO')
        self.cc_addrs = config.get_list('SMTP_CC') if config.get('SMTP_CC') else []
        self.bcc_addrs = config.get_list('SMTP_BCC') if config.get('SMTP_BCC') else []
        self.use_tls = config.get_bool('SMTP_USE_TLS', True)
        self.use_ssl = config.get_bool('SMTP_USE_SSL', False)
        self.subject = config.get('SMTP_SUBJECT', 'Grafana Dashboard Report')
        self.custom_body = config.get('EMAIL_BODY_MESSAGE')
        self.no_images_action = config.get('NO_IMAGES_ACTION', 'notify').lower()

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def send(self, image_paths: List[Path]) -> Tuple[bool, str]:
        """Send email with inline-embedded screenshot images.

        Args:
            image_paths: List of Path objects pointing to image files.

        Returns:
            Tuple of (success: bool, message: str).
        """
        try:
            return self._send_internal(image_paths)
        except Exception as e:
            # Never crash with unhandled exception
            logger.error(f"Unhandled email error: {e}")
            return False, f"Unhandled email error: {e}"

    def send_no_images_notification(self) -> Tuple[bool, str]:
        """Send a notification email when no images were captured.

        Only called when NO_IMAGES_ACTION=notify.
        """
        try:
            if not self._validate_smtp_config():
                return False, "SMTP configuration incomplete"

            msg = self._compose_no_images_message()
            self._send_via_smtp(msg)
            logger.info("No-images notification email sent")
            return True, "No-images notification sent"
        except Exception as e:
            logger.error(f"Failed to send no-images notification: {e}")
            return False, f"No-images notification failed: {e}"

    # -------------------------------------------------------------------------
    # Internal send logic
    # -------------------------------------------------------------------------

    def _send_internal(self, image_paths: List[Path]) -> Tuple[bool, str]:
        """Core send logic with full error handling."""
        # Handle no-images case
        if not image_paths:
            if self.no_images_action == 'notify':
                return self.send_no_images_notification()
            elif self.no_images_action == 'skip':
                logger.info("No images to send — skipping (NO_IMAGES_ACTION=skip)")
                return True, "No images, skipped"
            else:  # fail
                return False, "No images captured (NO_IMAGES_ACTION=fail)"

        # Validate files exist
        valid_paths = []
        for path in image_paths:
            if path.exists():
                valid_paths.append(path)
            else:
                logger.warning(f"Image file not found, skipping: {path}")

        if not valid_paths:
            logger.error("None of the image paths exist")
            return False, "All image paths are missing"

        # Check total file size against limit
        total_size = sum(p.stat().st_size for p in valid_paths)
        total_size_mb = total_size / (1024 * 1024)
        if total_size > MAX_ATTACHMENT_SIZE_BYTES:
            msg = (
                f"Total attachment size ({total_size_mb:.1f} MB) exceeds "
                f"{MAX_ATTACHMENT_SIZE_MB} MB limit — email not sent. "
                f"Reduce the number of panels or screenshot resolution."
            )
            logger.error(msg)
            return False, msg

        # Validate SMTP config
        if not self._validate_smtp_config():
            return False, "SMTP configuration incomplete"

        # Detect if files are PDFs (by extension)
        is_pdf = all(p.suffix.lower() == '.pdf' for p in valid_paths)

        logger.info("=" * 60)
        logger.info("SENDING EMAIL (SMTP_INTERNAL — HTML)")
        logger.info("=" * 60)
        logger.info(f"SMTP Server: {self.host}:{self.port}")
        logger.info(f"From: {self.from_addr}")
        logger.info(f"To: {', '.join(self.to_addrs)}")
        if self.cc_addrs:
            logger.info(f"CC: {', '.join(self.cc_addrs)}")
        if self.bcc_addrs:
            logger.info(f"BCC: {', '.join(self.bcc_addrs)}")
        logger.info(f"TLS: {self.use_tls} | SSL: {self.use_ssl}")
        logger.info(f"Auth: {'yes' if self.user else 'no (relay mode)'}")
        logger.info(f"Files: {len(valid_paths)} file(s) ({total_size_mb:.2f} MB) — {'PDF attachment' if is_pdf else 'inline embedded'}")

        try:
            if is_pdf:
                msg = self._compose_pdf_message(valid_paths)
            else:
                msg = self._compose_html_message(valid_paths)
            self._send_via_smtp(msg)
            logger.info("Email sent successfully")
            return True, "Email sent successfully"

        except smtplib.SMTPAuthenticationError:
            return False, "SMTP authentication failed — check SMTP_USER / SMTP_PASSWORD"
        except smtplib.SMTPConnectError as e:
            return False, f"SMTP connection failed: {e}"
        except smtplib.SMTPRecipientsRefused as e:
            return False, f"SMTP recipients refused: {e}"
        except smtplib.SMTPException as e:
            return False, f"SMTP error: {e}"
        except PermissionError as e:
            return False, f"Permission error reading image files: {e}"
        except FileNotFoundError as e:
            return False, f"Image file not found: {e}"
        except OSError as e:
            return False, f"OS error: {e}"

    def _validate_smtp_config(self) -> bool:
        """Check that minimum SMTP config is present."""
        if not self.host:
            logger.error("SMTP_HOST is not configured")
            return False
        if not self.from_addr:
            logger.error("SMTP_FROM is not configured")
            return False
        if not self.to_addrs or not self.to_addrs[0]:
            logger.error("SMTP_TO is not configured")
            return False
        return True

    # -------------------------------------------------------------------------
    # Message composition
    # -------------------------------------------------------------------------

    def _compose_html_message(self, image_paths: List[Path]) -> MIMEMultipart:
        """Build an HTML email with inline-embedded images.

        Structure:
          multipart/mixed
            └── multipart/related
                  ├── multipart/alternative
                  │     ├── text/plain  (fallback)
                  │     └── text/html   (primary)
                  ├── image (cid:img_0)
                  ├── image (cid:img_1)
                  └── ...
        """
        # Outer container
        msg = MIMEMultipart('mixed')
        msg['From'] = self.from_addr
        msg['To'] = ', '.join(self.to_addrs)
        if self.cc_addrs:
            msg['CC'] = ', '.join(self.cc_addrs)
        # BCC is intentionally NOT added to headers (handled in envelope)
        msg['Subject'] = self._get_subject_with_timestamp()

        # Related part (HTML + inline images)
        related = MIMEMultipart('related')

        # Alternative part (plain text + HTML)
        alternative = MIMEMultipart('alternative')

        # Plain text fallback
        plain_text = self._build_plain_text(image_paths)
        alternative.attach(MIMEText(plain_text, 'plain', 'utf-8'))

        # HTML body
        html_body = self._build_html_body(image_paths)
        alternative.attach(MIMEText(html_body, 'html', 'utf-8'))

        related.attach(alternative)

        # Attach inline images with Content-ID
        for idx, img_path in enumerate(image_paths):
            cid = f"img_{idx}"
            try:
                mime_type, _ = mimetypes.guess_type(str(img_path))
                if mime_type and mime_type.startswith('image/'):
                    subtype = mime_type.split('/')[1]
                else:
                    subtype = 'png'

                with open(img_path, 'rb') as f:
                    # M8: Warn if image is very large (may exceed SMTP server limits)
                    file_size_mb = img_path.stat().st_size / (1024 * 1024)
                    if file_size_mb > 10:
                        logger.warning(f"Image {img_path.name} is {file_size_mb:.1f} MB — may exceed SMTP limits")
                    img_data = f.read()

                img_part = MIMEImage(img_data, _subtype=subtype)
                img_part.add_header('Content-ID', f'<{cid}>')
                img_part.add_header('Content-Disposition', 'inline', filename=img_path.name)
                related.attach(img_part)
            except Exception as e:
                logger.warning(f"Failed to embed image {img_path.name}: {e}")

        msg.attach(related)
        return msg

    def _compose_pdf_message(self, pdf_paths: List[Path]) -> MIMEMultipart:
        """Build an HTML email with PDF files as attachments.

        Structure:
          multipart/mixed
            ├── multipart/alternative
            │     ├── text/plain  (fallback)
            │     └── text/html   (primary)
            ├── application/pdf (attachment 1)
            ├── application/pdf (attachment 2)
            └── ...
        """
        # Outer container
        msg = MIMEMultipart('mixed')
        msg['From'] = self.from_addr
        msg['To'] = ', '.join(self.to_addrs)
        if self.cc_addrs:
            msg['CC'] = ', '.join(self.cc_addrs)
        msg['Subject'] = self._get_subject_with_timestamp()

        # Alternative part (plain text + HTML)
        alternative = MIMEMultipart('alternative')

        # Plain text fallback
        plain_text = self._build_plain_text_pdf(pdf_paths)
        alternative.attach(MIMEText(plain_text, 'plain', 'utf-8'))

        # HTML body
        html_body = self._build_html_body_pdf(pdf_paths)
        alternative.attach(MIMEText(html_body, 'html', 'utf-8'))

        msg.attach(alternative)

        # Attach PDF files
        for pdf_path in pdf_paths:
            try:
                with open(pdf_path, 'rb') as f:
                    pdf_data = f.read()
                pdf_part = MIMEBase('application', 'pdf')
                pdf_part.set_payload(pdf_data)
                encoders.encode_base64(pdf_part)
                pdf_part.add_header(
                    'Content-Disposition', 'attachment',
                    filename=pdf_path.name
                )
                msg.attach(pdf_part)
                file_size_mb = len(pdf_data) / (1024 * 1024)
                logger.info(f"  Attached PDF: {pdf_path.name} ({file_size_mb:.2f} MB)")
            except Exception as e:
                logger.warning(f"Failed to attach PDF {pdf_path.name}: {e}")

        return msg

    def _compose_no_images_message(self) -> MIMEMultipart:
        """Build a notification email for when no images were captured."""
        msg = MIMEMultipart('alternative')
        msg['From'] = self.from_addr
        msg['To'] = ', '.join(self.to_addrs)
        if self.cc_addrs:
            msg['CC'] = ', '.join(self.cc_addrs)
        msg['Subject'] = self._get_subject_with_timestamp() + ' [No Images]'

        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        dashboard_uid = self.config.get('GRAFANA_DASHBOARD_UID', 'Unknown')

        plain = (
            f"GrafMail-Grafana Dashboard Report — No Images Captured\n"
            f"================================================\n\n"
            f"Timestamp: {timestamp}\n"
            f"Dashboard UID: {dashboard_uid}\n\n"
            f"No screenshots were captured during this run.\n"
            f"Please check the tool logs for errors.\n"
        )

        html = f"""\
<html>
<body style="font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f4; padding: 20px;">
  <div style="max-width: 600px; margin: 0 auto; background: #fff; border-radius: 8px;
              box-shadow: 0 2px 8px rgba(0,0,0,0.1); overflow: hidden;">
    <div style="background: #e74c3c; color: #fff; padding: 20px 24px;">
      <h2 style="margin: 0; font-size: 20px;">⚠ No Images Captured</h2>
    </div>
    <div style="padding: 24px;">
      <p style="color: #555; margin: 0 0 12px;"><strong>Timestamp:</strong> {timestamp}</p>
      <p style="color: #555; margin: 0 0 12px;"><strong>Dashboard UID:</strong> {dashboard_uid}</p>
      <p style="color: #888; margin: 16px 0 0;">No screenshots were captured during this run.
         Please check the tool logs for errors.</p>
    </div>
  </div>
</body>
</html>"""

        msg.attach(MIMEText(plain, 'plain', 'utf-8'))
        msg.attach(MIMEText(html, 'html', 'utf-8'))
        return msg

    # -------------------------------------------------------------------------
    # Body builders
    # -------------------------------------------------------------------------

    def _build_plain_text(self, image_paths: List[Path]) -> str:
        """Plain-text fallback body."""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        dashboard_uid = self.config.get('GRAFANA_DASHBOARD_UID', 'Unknown')
        grafana_url = self.config.get('GRAFANA_URL', 'Unknown')
        time_from = self.config.get('DASHBOARD_TIME_FROM', 'Unknown')
        time_to = self.config.get('DASHBOARD_TIME_TO', 'Unknown')
        file_list = "\n".join([f"  - {p.name}" for p in image_paths])

        if self.custom_body:
            return (
                f"{self.custom_body}\n\n"
                f"Images ({len(image_paths)}):\n{file_list}\n"
            )

        return (
            f"GrafMail-Grafana Dashboard Report\n"
            f"========================\n\n"
            f"Generated: {timestamp}\n"
            f"Dashboard UID: {dashboard_uid}\n"
            f"Source: {grafana_url}\n"
            f"Time Range: {time_from} to {time_to}\n\n"
            f"Images ({len(image_paths)} file(s)):\n{file_list}\n\n"
            f"Note: This is the plain-text version. View in an HTML-capable\n"
            f"email client to see the embedded screenshots.\n"
        )

    def _build_plain_text_pdf(self, pdf_paths: List[Path]) -> str:
        """Plain-text fallback body for PDF attachments."""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        dashboard_uid = self.config.get('GRAFANA_DASHBOARD_UID', 'Unknown')
        grafana_url = self.config.get('GRAFANA_URL', 'Unknown')
        time_from = self.config.get('DASHBOARD_TIME_FROM', 'Unknown')
        time_to = self.config.get('DASHBOARD_TIME_TO', 'Unknown')
        file_list = "\n".join([f"  - {p.name} ({p.stat().st_size / (1024*1024):.2f} MB)" for p in pdf_paths])

        if self.custom_body:
            return (
                f"{self.custom_body}\n\n"
                f"PDF Attachments ({len(pdf_paths)}):\n{file_list}\n"
            )

        return (
            f"GrafMail-Grafana Dashboard Report\n"
            f"========================\n\n"
            f"Generated: {timestamp}\n"
            f"Dashboard UID: {dashboard_uid}\n"
            f"Source: {grafana_url}\n"
            f"Time Range: {time_from} to {time_to}\n\n"
            f"PDF Attachments ({len(pdf_paths)} file(s)):\n{file_list}\n\n"
            f"The PDF report is attached to this email.\n"
        )

    def _build_html_body_pdf(self, pdf_paths: List[Path]) -> str:
        """Build the HTML body for PDF attachment emails."""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        dashboard_uid = self.config.get('GRAFANA_DASHBOARD_UID', 'Unknown')
        grafana_url = self.config.get('GRAFANA_URL', 'Unknown')
        time_from = self.config.get('DASHBOARD_TIME_FROM', 'Unknown')
        time_to = self.config.get('DASHBOARD_TIME_TO', 'Unknown')

        # Custom body or default header
        if self.custom_body:
            header_text = html_module.escape(self.custom_body).replace('\n', '<br>')
        else:
            header_text = ""

        # Build PDF list
        pdf_items = ""
        for pdf_path in pdf_paths:
            label = html_module.escape(pdf_path.name)
            size_mb = pdf_path.stat().st_size / (1024 * 1024)
            pdf_items += f"""\
    <tr>
      <td style="padding: 8px 14px; border-bottom: 1px solid #eee;">
        📄 {label}
      </td>
      <td style="padding: 8px 14px; border-bottom: 1px solid #eee; text-align: right; color: #888;">
        {size_mb:.2f} MB
      </td>
    </tr>
"""

        html = f"""\
<html>
<body style="font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f4; padding: 20px; margin: 0;">
  <div style="max-width: 960px; margin: 0 auto; background: #ffffff; border-radius: 8px;
              box-shadow: 0 2px 12px rgba(0,0,0,0.1); overflow: hidden;">

    <!-- Header -->
    <div style="background: linear-gradient(135deg, #2c3e50, #3498db); color: #fff;
                padding: 24px 28px;">
      <h1 style="margin: 0 0 6px; font-size: 22px; color: purple;">📊 Grafana Dashboard Report - GrafMail</h1>
      <p style="margin: 0; font-size: 13px; opacity: 0.85;">{timestamp}</p>
    </div>

    <!-- Metadata -->
    <div style="padding: 20px 28px; border-bottom: 1px solid #eee;">
      <table style="width: 100%; font-size: 14px; color: #444; border-collapse: collapse;">
        <tr>
          <td style="padding: 4px 0;"><strong>Dashboard UID:</strong></td>
          <td style="padding: 4px 0;">{html_module.escape(str(dashboard_uid))}</td>
        </tr>
        <tr>
          <td style="padding: 4px 0;"><strong>Report Name :</strong></td>
          <td style="padding: 4px 0;">{html_module.escape(str(self.subject))}</td>
        </tr>
        <tr>
          <td style="padding: 4px 0;"><strong>Source:</strong></td>
          <td style="padding: 4px 0;">{html_module.escape(str(grafana_url))}</td>
        </tr>
        <tr>
          <td style="padding: 4px 0;"><strong>Time Range:</strong></td>
          <td style="padding: 4px 0;">{html_module.escape(str(time_from))} → {html_module.escape(str(time_to))}</td>
        </tr>
        <tr>
          <td style="padding: 4px 0;"><strong>Attachments:</strong></td>
          <td style="padding: 4px 0;">{len(pdf_paths)} PDF file(s)</td>
        </tr>
      </table>
      {"<div style='margin-top: 12px; color: #555; font-size: 14px;'>" + header_text + "</div>" if header_text else ""}
    </div>

    <!-- PDF Attachments List -->
    <div style="padding: 24px 28px;">
      <h2 style="font-size: 16px; color: #2c3e50; margin: 0 0 16px; border-bottom: 2px solid #3498db;
                 padding-bottom: 8px;">PDF Attachments</h2>
      <table style="width: 100%; font-size: 14px; color: #444; border-collapse: collapse;">
{pdf_items}
      </table>
      <p style="margin: 16px 0 0; color: #888; font-size: 13px;">
        Please find the PDF report(s) attached to this email.
      </p>
    </div>

    <!-- Footer -->
    <div style="background: #f8f9fa; padding: 14px 28px; text-align: center;
                font-size: 12px; color: #999; border-top: 1px solid #eee;">
      Generated by Grafana Screenshot Tool &bull; {timestamp}
    </div>
  </div>
</body>
</html>"""
        return html

    def _build_html_body(self, image_paths: List[Path]) -> str:
        """Build the HTML body with inline-embedded images."""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        dashboard_uid = self.config.get('GRAFANA_DASHBOARD_UID', 'Unknown')
        grafana_url = self.config.get('GRAFANA_URL', 'Unknown')
        time_from = self.config.get('DASHBOARD_TIME_FROM', 'Unknown')
        time_to = self.config.get('DASHBOARD_TIME_TO', 'Unknown')

        # Custom body or default header
        if self.custom_body:
            header_text = html_module.escape(self.custom_body).replace('\n', '<br>')
        else:
            header_text = ""

        # Build image cards
        image_cards = ""
        for idx, img_path in enumerate(image_paths):
            cid = f"img_{idx}"
            label = html_module.escape(img_path.name)
            image_cards += f"""\
    <div style="margin-bottom: 24px; border: 1px solid #e0e0e0; border-radius: 6px;
                overflow: hidden; background: #fafafa;">
      <div style="background: #34495e; color: #fff; padding: 8px 14px;
                  font-size: 13px; font-family: monospace;">
        📷 {label}
      </div>
      <div style="padding: 12px; text-align: center;">
        <img src="cid:{cid}" alt="{label}"
             style="max-width: 100%; height: auto; border-radius: 4px;
                    box-shadow: 0 1px 4px rgba(0,0,0,0.15);" />
      </div>
    </div>
"""

        html = f"""\
<html>
<body style="font-family: 'Segoe UI', Arial, sans-serif; background: #f4f4f4; padding: 20px; margin: 0;">
  <div style="max-width: 960px; margin: 0 auto; background: #ffffff; border-radius: 8px;
              box-shadow: 0 2px 12px rgba(0,0,0,0.1); overflow: hidden;">

    <!-- Header -->
    <div style="background: linear-gradient(135deg, #2c3e50, #3498db); color: #fff;
                padding: 24px 28px;">
      <h1 style="margin: 0 0 6px; font-size: 22px; color: purple;">📊 Grafana Dashboard Report - GrafMail</h1>
      <p style="margin: 0; font-size: 13px; opacity: 0.85;">{timestamp}</p>
    </div>

    <!-- Metadata -->
    <div style="padding: 20px 28px; border-bottom: 1px solid #eee;">
      <table style="width: 100%; font-size: 14px; color: #444; border-collapse: collapse;">
        <tr>
          <td style="padding: 4px 0;"><strong>Dashboard UID:</strong></td>
          <td style="padding: 4px 0;">{html_module.escape(str(dashboard_uid))}</td>
        </tr>
        <tr>
          <td style="padding: 4px 0;"><strong>Report Name :</strong></td>
          <td style="padding: 4px 0;">{html_module.escape(str(self.subject))}</td>
        </tr>
        <tr>
          <td style="padding: 4px 0;"><strong>Source:</strong></td>
          <td style="padding: 4px 0;">{html_module.escape(str(grafana_url))}</td>
        </tr>
        <tr>
          <td style="padding: 4px 0;"><strong>Time Range:</strong></td>
          <td style="padding: 4px 0;">{html_module.escape(str(time_from))} → {html_module.escape(str(time_to))}</td>
        </tr>
        <tr>
          <td style="padding: 4px 0;"><strong>Images:</strong></td>
          <td style="padding: 4px 0;">{len(image_paths)} file(s)</td>
        </tr>
      </table>
      {"<div style='margin-top: 12px; color: #555; font-size: 14px;'>" + header_text + "</div>" if header_text else ""}
    </div>

    <!-- Screenshots -->
    <div style="padding: 24px 28px;">
      <h2 style="font-size: 16px; color: #2c3e50; margin: 0 0 16px; border-bottom: 2px solid #3498db;
                 padding-bottom: 8px;">Screenshots</h2>
{image_cards}
    </div>

    <!-- Footer -->
    <div style="background: #f8f9fa; padding: 14px 28px; text-align: center;
                font-size: 12px; color: #999; border-top: 1px solid #eee;">
      Generated by Grafana Screenshot Tool &bull; {timestamp}
    </div>
  </div>
</body>
</html>"""
        return html

    # -------------------------------------------------------------------------
    # Helpers
    # -------------------------------------------------------------------------

    def _get_subject_with_timestamp(self) -> str:
        """Append a human-readable timestamp to the configured subject."""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M')
        return f"{self.subject} - {timestamp}"

    def _get_all_recipients(self) -> List[str]:
        """Return all recipients (TO + CC + BCC) for the SMTP envelope."""
        all_addrs = list(self.to_addrs)
        all_addrs.extend(self.cc_addrs)
        all_addrs.extend(self.bcc_addrs)
        return [addr.strip() for addr in all_addrs if addr.strip()]

    def _send_via_smtp(self, msg: MIMEMultipart) -> None:
        """Open an SMTP connection and send the message.

        Three modes:
          1. SSL        — SMTP_SSL on port 465   (SMTP_USE_SSL=true)
          2. TLS + Auth — STARTTLS on port 587   (SMTP_USE_TLS=true)
          3. Plain relay — no encryption          (both false)
        """
        all_recipients = self._get_all_recipients()

        if self.use_ssl:
            # SSL mode (port 465)
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(self.host, self.port, timeout=60, context=context) as server:
                if self.user and self.password:
                    server.login(self.user, self.password)
                    self.password = None  
                server.sendmail(self.from_addr, all_recipients, msg.as_string())
                logger.info("Email sent via SSL (port 465)")

        elif self.use_tls:
            # STARTTLS mode (port 587)
            with smtplib.SMTP(self.host, self.port, timeout=60) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                if self.user and self.password:
                    server.login(self.user, self.password)
                    self.password = None  
                server.sendmail(self.from_addr, all_recipients, msg.as_string())
                logger.info("Email sent via STARTTLS")

        else:
            # Plain relay (port 25, no encryption)
            with smtplib.SMTP(self.host, self.port, timeout=60) as server:
                if self.user and self.password:
                    server.login(self.user, self.password)
                    self.password = None  
                server.sendmail(self.from_addr, all_recipients, msg.as_string())
                logger.info("Email sent via plain SMTP (no encryption)")
