"""Email service for sending PDF reports."""
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import os
from typing import Optional


class EmailConfig:
    """Configuration for email sending."""

    def __init__(
        self,
        sender_email: str,
        sender_password: str,
        smtp_server: str = "smtp.qq.com",
        smtp_port: int = 465
    ):
        self.sender_email = sender_email
        self.sender_password = sender_password
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port


class EmailService:
    """Service for sending emails with PDF attachments."""

    def __init__(self, config: EmailConfig):
        """
        Initialize email service.

        Args:
            config: Email configuration
        """
        self.config = config

    def send_with_pdf(
        self,
        file_path: str,
        recipient_email: str,
        subject: Optional[str] = None,
        body: str = "这是自动生成的情报简报，请查收。"
    ) -> bool:
        """
        Send email with PDF attachment.

        Args:
            file_path: Path to PDF file
            recipient_email: Recipient email address
            subject: Email subject (auto-generated if None)
            body: Email body text

        Returns:
            True if successful, False otherwise
        """
        if not os.path.exists(file_path):
            print(f"❌ 文件不存在: {file_path}")
            return False

        filename = os.path.basename(file_path)

        # Create email message
        msg = MIMEMultipart()
        msg['From'] = self.config.sender_email
        msg['To'] = recipient_email
        msg['Subject'] = subject or f"情报简报 - {filename}"

        # Add body
        msg.attach(MIMEText(body, 'plain', 'utf-8'))

        # Attach PDF
        try:
            with open(file_path, "rb") as attachment:
                part = MIMEBase("application", "pdf")
                part.set_payload(attachment.read())

            encoders.encode_base64(part)
            part.add_header(
                "Content-Disposition",
                'attachment', filename=('utf-8', '', filename),
            )
            msg.attach(part)

        except Exception as e:
            print(f"❌ 读取附件失败: {type(e).__name__}")
            return False

        # Send email
        try:
            with smtplib.SMTP_SSL(self.config.smtp_server, self.config.smtp_port, timeout=30) as server:
                server.login(self.config.sender_email, self.config.sender_password)
                server.sendmail(self.config.sender_email, recipient_email, msg.as_string())
            print(f"📧 邮件已成功发送至: {recipient_email}")
            return True

        except Exception as e:
            print(f"❌ 邮件发送失败: {type(e).__name__}")
            return False
