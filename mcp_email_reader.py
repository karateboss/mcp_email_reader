from fastmcp import FastMCP
from imapclient import IMAPClient
from datetime import datetime
import email
from email.header import decode_header
import os
import base64
from cryptography.fernet import Fernet
from dotenv import load_dotenv

# Load the .env file in the python root dir. This should contain the password secret key
load_dotenv()

# Initialize MCP Server
mcp = FastMCP("Email MCP Server")

# Email Server Configuration
IMAP_SERVER = os.getenv("IMAP_SERVER")
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT")
#encoded_pw = os.getenv("EMAIL_PASSWORD")
#EMAIL_PASSWORD = base64.b64decode(encoded_pw).decode()  # decode base64 → bytes → str
encrypted_pw = os.getenv("EMAIL_PASSWORD_ENC")
secret_key = os.getenv("EMAIL_SECRET_KEY")

fernet = Fernet(secret_key.encode())
EMAIL_PASSWORD = fernet.decrypt(encrypted_pw.encode()).decode()

def connect_to_email():
    """Connects to the IMAP email server."""
    try:
        server = IMAPClient(IMAP_SERVER, ssl=True)
        server.login(EMAIL_ACCOUNT, EMAIL_PASSWORD)
        return server
    except Exception as e:
        return f"Error connecting to email server: {str(e)}"


def decode_mime_words(s):
    """Helper to decode MIME-encoded words in headers"""
    decoded = decode_header(s)
    return ''.join([
        part.decode(enc or 'utf-8') if isinstance(part, bytes) else part
        for part, enc in decoded
    ])

def extract_email_bodies(msg):
    """
    Extract both plain text and HTML bodies from an email message.
    Returns a dict: {"text": "...", "html": "..."}
    """
    text_body = ""
    html_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_dispo = str(part.get("Content-Disposition", ""))

            if "attachment" not in content_dispo:
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"

                    if content_type == "text/plain" and not text_body:
                        text_body = payload.decode(charset, errors="ignore")
                    elif content_type == "text/html" and not html_body:
                        html_body = payload.decode(charset, errors="ignore")
                except Exception:
                    continue
    else:
        content_type = msg.get_content_type()
        charset = msg.get_content_charset() or "utf-8"
        payload = msg.get_payload(decode=True)

        if content_type == "text/plain":
            text_body = payload.decode(charset, errors="ignore")
        elif content_type == "text/html":
            html_body = payload.decode(charset, errors="ignore")

    return {
        "text": text_body.strip() or "(No plain text content found)",
        "html": html_body.strip() or "(No HTML content found)"
    }

def get_attachment_names(msg):
    """Returns list of attachment filenames"""
    attachments = []
    for part in msg.walk():
        if part.get_content_disposition() == "attachment":
            filename = part.get_filename()
            if filename:
                attachments.append(decode_mime_words(filename))
    return attachments

@mcp.tool()
def search_emails(
    search_string: str,
    folder: str = "INBOX",
    limit: int = 10,
    since_date: str = None,
    before_date: str = None,
    sort_ascending: bool = False,
    include_html: bool = False,
    sender_filter: str = None,
    has_attachment: bool = False
) -> list:
    """
    Searches emails with keyword, date range, sender, and attachment filtering.

    :param search_string: Keyword to search in the email.
    :param folder: IMAP folder to search in.
    :param limit: Max number of results.
    :param since_date: Only include emails since this date (YYYY-MM-DD).
    :param before_date: Only include emails before this date (YYYY-MM-DD).
    :param sort_ascending: If True, return oldest emails first.
    :param include_html: If True, include full HTML content.
    :param sender_filter: Filter by sender email address.
    :param has_attachment: If True, return only emails with attachments.
    :return: List of matching email summaries.
    """
    try:
        server = connect_to_email()
        if isinstance(server, str):
            return [server]

        server.select_folder(folder)

        # Build IMAP search criteria
        search_criteria = []

        if sender_filter:
            search_criteria.extend([b'FROM', sender_filter.encode()])

        if search_string:
            search_criteria.extend([b'TEXT', search_string.encode()])

        if since_date:
            try:
                since_dt = datetime.strptime(since_date, "%Y-%m-%d").date()
                search_criteria.extend([b'SINCE', since_dt.strftime("%d-%b-%Y").encode()])
            except ValueError:
                return [f"Invalid format for since_date. Use YYYY-MM-DD."]

        if before_date:
            try:
                before_dt = datetime.strptime(before_date, "%Y-%m-%d").date()
                search_criteria.extend([b'BEFORE', before_dt.strftime("%d-%b-%Y").encode()])
            except ValueError:
                return [f"Invalid format for before_date. Use YYYY-MM-DD."]

        # Search emails
        messages = server.search(search_criteria)
        messages = sorted(messages, reverse=not sort_ascending)

        email_list = []
        for msg_id in messages:
            if len(email_list) >= limit:
                break

            raw_msg = server.fetch(msg_id, ["RFC822"])[msg_id][b"RFC822"]
            msg = email.message_from_bytes(raw_msg)

            attachments = get_attachment_names(msg)

            # Skip if filtering by attachments and none are found
            if has_attachment and not attachments:
                continue

            subject = decode_mime_words(msg.get("Subject", "(No Subject)"))
            sender = msg.get("From", "Unknown Sender")
            date = msg.get("Date", "Unknown Date")
            bodies = extract_email_bodies(msg)

            email_data = {
                "subject": subject,
                "sender": sender,
                "date": date,
                "body": bodies["text"][:500],
                "attachments": attachments or []
            }

            if include_html:
                email_data["body_html"] = bodies["html"]

            email_list.append(email_data)

        return email_list if email_list else ["No emails found."]

    except Exception as e:
        return [f"Error searching emails: {str(e)}"]

@mcp.tool()
def download_attachment(
    search_string: str,
    folder: str = "INBOX",
    sender_filter: str = None,
    since_date: str = None,
    attachment_name: str = None,
    download_dir: str = "./downloads"
) -> list:
    """
    Downloads attachment(s) from the first matching email.

    :param search_string: Keyword to find the target email.
    :param folder: IMAP folder to search.
    :param sender_filter: Optional sender email to filter by.
    :param since_date: Optional date (YYYY-MM-DD) to start search from.
    :param attachment_name: Optional filename to filter attachments.
    :param download_dir: Directory to save attachments.
    :return: List of downloaded filenames or error messages.
    """
    try:
        server = connect_to_email()
        if isinstance(server, str):
            return [server]

        server.select_folder(folder)

        # Build IMAP search criteria
        search_criteria = []

        if sender_filter:
            search_criteria.extend([b'FROM', sender_filter.encode()])
        if search_string:
            search_criteria.extend([b'TEXT', search_string.encode()])
        if since_date:
            try:
                since_dt = datetime.strptime(since_date, "%Y-%m-%d").date()
                search_criteria.extend([b'SINCE', since_dt.strftime("%d-%b-%Y").encode()])
            except ValueError:
                return [f"Invalid date format for 'since_date'. Use YYYY-MM-DD."]

        messages = server.search(search_criteria)
        if not messages:
            return [f"No matching email found for: '{search_string}'"]

        msg_id = messages[0]  # only look at the first match
        raw_msg = server.fetch(msg_id, ["RFC822"])[msg_id][b"RFC822"]
        msg = email.message_from_bytes(raw_msg)

        os.makedirs(download_dir, exist_ok=True)
        saved_files = []

        for part in msg.walk():
            if part.get_content_disposition() == "attachment":
                filename = part.get_filename()
                if not filename:
                    continue
                decoded_filename = decode_mime_words(filename)

                # If user specified a target filename, skip others
                if attachment_name and attachment_name not in decoded_filename:
                    continue

                file_path = os.path.join(download_dir, decoded_filename)
                with open(file_path, "wb") as f:
                    f.write(part.get_payload(decode=True))
                saved_files.append(decoded_filename)

        return saved_files if saved_files else ["No matching attachment found."]

    except Exception as e:
        return [f"Error downloading attachment: {str(e)}"]


@mcp.tool()
def list_folders() -> list:
    """
    Lists all available folders/mailboxes on the email server.

    :return: A list of folder names or an error message.
    """
    try:
        server = connect_to_email()
        if isinstance(server, str):
            return [server]  # Return connection error

        folders = server.list_folders()
        # folders is a list of (flags, delimiter, folder_name)
        folder_names = [folder[2] for folder in folders]

        return folder_names if folder_names else ["No folders found."]
    except Exception as e:
        return [f"Error listing folders: {str(e)}"]


# Run the MCP Server
def main():
    mcp.run()


if __name__ == "__main__":
    main()
