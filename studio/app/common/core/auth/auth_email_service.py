"""
Email service for sending verification and authentication emails.
"""

from firebase_admin import auth as firebase_auth
from requests.exceptions import HTTPError

from studio.app.common.core.auth import pyrebase_app
from studio.app.common.core.auth.auth import _extract_firebase_error
from studio.app.common.core.logger import AppLogger
from studio.app.common.core.utils.config_handler import get_env_bool
from studio.app.const import FRONTEND_URL

try:
    from studio.app.common.core.auth.firebase_email_sender import (
        send_password_reset_email_via_firebase,
        send_verification_email_via_firebase,
    )

    FIREBASE_EMAIL_AVAILABLE = True
except ImportError:
    FIREBASE_EMAIL_AVAILABLE = False

logger = AppLogger.get_logger()

# Determine which email sending method to use
USE_FIREBASE_EMAIL = get_env_bool("USE_FIREBASE_EMAIL", default=True)


class AuthEmailService:
    """
    Service for sending authentication-related emails via Firebase.

    Handles email verification and password reset emails for user authentication.
    Sends via Firebase's built-in email service; password reset falls back to
    Pyrebase when that module is unavailable. `USE_FIREBASE_EMAIL=False` selects
    development mode, which logs the link and sends nothing.
    """

    @staticmethod
    def send_verification_email(email: str) -> bool:
        """
        Send email verification link to user using Firebase's built-in email service.

        No SMTP configuration required! Firebase sends the email automatically
        using the template configured in Firebase Console.

        Args:
            email: User's email address (must be an existing Firebase user)

        Returns:
            bool: True if email was sent successfully, False otherwise

        Raises:
            Exception: If email sending fails
        """
        try:
            # Use Firebase's built-in email service via REST API
            if USE_FIREBASE_EMAIL and FIREBASE_EMAIL_AVAILABLE:
                logger.info(f"Sending verification email via Firebase to {email}")
                send_verification_email_via_firebase(email)
                return True

            # Development/Testing: Just log the link
            else:
                action_code_settings = {
                    "url": f"{FRONTEND_URL}/login",
                    "handleCodeInApp": False,
                }

                verification_link = firebase_auth.generate_email_verification_link(
                    email, action_code_settings=action_code_settings
                )

                logger.warning(
                    "Firebase email service not configured. "
                    "Add FIREBASE_API_KEY to .env to enable automatic email sending."
                )
                logger.debug(f"Verification link for {email}: {verification_link}")
                logger.debug(
                    "Copy this link and paste it in your browser to verify the email."
                )
                return True

        except Exception as e:
            logger.error(
                f"Failed to send verification email to {email}: {e}", exc_info=True
            )
            raise

    @staticmethod
    def send_password_reset_email(email: str) -> bool:
        """
        Send password reset email to user using Firebase's built-in email service.

        Args:
            email: User's email address

        Returns:
            bool: True if email was sent successfully

        Raises:
            Exception: If email sending fails
        """
        try:
            # Use Firebase's built-in email service via REST API
            if USE_FIREBASE_EMAIL and FIREBASE_EMAIL_AVAILABLE:
                logger.info(f"Sending password reset email via Firebase to {email}")
                send_password_reset_email_via_firebase(email)
                return True

            # Fallback: Use Pyrebase (works directly for password reset!)
            # Gated on the flag too, so USE_FIREBASE_EMAIL=False sends no mail at
            # all rather than only redirecting which service sends it.
            elif USE_FIREBASE_EMAIL and pyrebase_app:
                logger.info(f"Sending password reset email via Pyrebase to {email}")
                pyrebase_app.auth().send_password_reset_email(email)
                return True

            # Development/Testing: Just log the link
            else:
                action_code_settings = {
                    "url": f"{FRONTEND_URL}/reset-password",
                    "handleCodeInApp": False,
                }

                reset_link = firebase_auth.generate_password_reset_link(
                    email, action_code_settings=action_code_settings
                )

                logger.warning(
                    "Firebase email service not configured. "
                    "Add FIREBASE_API_KEY to .env to enable automatic email sending."
                )
                logger.debug(f"Password reset link for {email}: {reset_link}")
                logger.debug(
                    "Copy this link and paste it in your browser to reset password."
                )
                return True

        except Exception as e:
            if isinstance(e, HTTPError):
                # Traceback and str(e) embed the request URL with the API key.
                message = _extract_firebase_error(e)
                logger.error(
                    f"Failed to send password reset email to {email}: {message}"
                )
            else:
                logger.error(
                    f"Failed to send password reset email to {email}: {e}",
                    exc_info=True,
                )
            raise
