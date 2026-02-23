"""
Built-in Email Templates
========================

This module provides hard-coded email templates that are always available,
independent of the database. These serve as fallbacks and quick-access templates.

Template Format:
- Placeholders use {{placeholder_name}} syntax
- All templates have a unique key for reference
- Templates include both text and optional HTML versions

Usage in Agent:
    The agent can reference templates by their key:
    - "meeting_confirmation" - For calendar booking confirmations
    - "follow_up_basic" - Simple follow-up after a call
    - "custom" - Agent composes the email freely

The agent should:
1. Identify which template fits the situation
2. Gather required placeholder values from the conversation
3. Call the email tool with template_key and placeholder values
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from livekit.agents.llm import function_tool


@dataclass(frozen=True)
class BuiltinTemplate:
    """Represents a built-in email template."""
    key: str
    name: str
    category: str
    subject_template: str
    text_body_template: str
    html_body_template: str | None = None
    placeholders: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""


# ============================================================================
# MEETING INVITATION TEMPLATES
# ============================================================================

MEETING_CONFIRMATION = BuiltinTemplate(
    key="meeting_confirmation",
    name="Meeting Confirmation",
    category="meeting_invite",
    subject_template="Meeting Confirmed: {{meeting_title}}",
    text_body_template="""Hi {{recipient_name}},

This is to confirm your meeting has been scheduled.

Meeting Details:
- Title: {{meeting_title}}
- Date & Time: {{meeting_datetime}}
- Duration: {{meeting_duration}}
- Join Link: {{meeting_link}}

Looking forward to speaking with you!

Best regards,
{{sender_name}}""",
    html_body_template="""<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
<p>Hi {{recipient_name}},</p>

<p>This is to confirm your meeting has been scheduled.</p>

<div style="background-color: #f5f5f5; padding: 15px; border-radius: 5px; margin: 20px 0;">
<h3 style="margin-top: 0; color: #2c5282;">Meeting Details</h3>
<p><strong>Title:</strong> {{meeting_title}}</p>
<p><strong>Date & Time:</strong> {{meeting_datetime}}</p>
<p><strong>Duration:</strong> {{meeting_duration}}</p>
<p><strong>Join Link:</strong> <a href="{{meeting_link}}" style="color: #2b6cb0;">{{meeting_link}}</a></p>
</div>

<p>Looking forward to speaking with you!</p>

<p>Best regards,<br>
<strong>{{sender_name}}</strong></p>
</body>
</html>""",
    placeholders=(
        "recipient_name", "meeting_title", "meeting_datetime",
        "meeting_duration", "meeting_link", "sender_name"
    ),
    description="Standard meeting confirmation with Google Meet link. Use after booking a calendar slot.",
)


QUICK_INVITE = BuiltinTemplate(
    key="quick_invite",
    name="Quick Meeting Invite",
    category="meeting_invite",
    subject_template="You're invited: {{meeting_title}}",
    text_body_template="""Hi {{recipient_name}},

You're invited to join a meeting.

When: {{meeting_datetime}}
Join here: {{meeting_link}}

See you there!

{{sender_name}}""",
    placeholders=(
        "recipient_name", "meeting_title", "meeting_datetime",
        "meeting_link", "sender_name"
    ),
    description="Short, concise meeting invitation. Use for quick scheduling.",
)


# ============================================================================
# FOLLOW-UP TEMPLATES
# ============================================================================

FOLLOW_UP_BASIC = BuiltinTemplate(
    key="follow_up_basic",
    name="Basic Follow-Up",
    category="follow_up",
    subject_template="Following up on our conversation",
    text_body_template="""Hi {{recipient_name}},

Thank you for taking the time to speak with me today.

{{message_body}}

Please don't hesitate to reach out if you have any questions.

Best regards,
{{sender_name}}""",
    placeholders=("recipient_name", "message_body", "sender_name"),
    description="Simple follow-up email. Agent fills in the message_body based on conversation.",
)


FOLLOW_UP_WITH_SUMMARY = BuiltinTemplate(
    key="follow_up_summary",
    name="Follow-Up with Summary",
    category="follow_up",
    subject_template="Summary: Our conversation about {{topic}}",
    text_body_template="""Hi {{recipient_name}},

Thank you for your time today. Here's a quick summary of what we discussed:

{{call_summary}}

Next Steps:
{{next_steps}}

If you have any questions or need clarification on anything, please let me know.

Best regards,
{{sender_name}}
{{company_name}}""",
    html_body_template="""<!DOCTYPE html>
<html>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
<p>Hi {{recipient_name}},</p>

<p>Thank you for your time today. Here's a quick summary of what we discussed:</p>

<div style="background-color: #f0f4f8; padding: 15px; border-left: 4px solid #4a90d9; margin: 15px 0;">
{{call_summary}}
</div>

<h4 style="color: #2c5282;">Next Steps:</h4>
<p>{{next_steps}}</p>

<p>If you have any questions or need clarification on anything, please let me know.</p>

<p>Best regards,<br>
<strong>{{sender_name}}</strong><br>
{{company_name}}</p>
</body>
</html>""",
    placeholders=(
        "recipient_name", "topic", "call_summary",
        "next_steps", "sender_name", "company_name"
    ),
    description="Detailed follow-up with call summary and next steps. Use after substantive conversations.",
)


# ============================================================================
# INFORMATION & RESOURCES TEMPLATES
# ============================================================================

SEND_INFORMATION = BuiltinTemplate(
    key="send_info",
    name="Send Information",
    category="information",
    subject_template="Information you requested: {{topic}}",
    text_body_template="""Hi {{recipient_name}},

As promised, here's the information you requested about {{topic}}:

{{information_content}}

{{#if resource_links}}
You may also find these resources helpful:
{{resource_links}}
{{/if}}

Let me know if you need anything else!

Best regards,
{{sender_name}}""",
    placeholders=(
        "recipient_name", "topic", "information_content",
        "resource_links", "sender_name"
    ),
    description="Send requested information or documents. Use when prospect asks for details.",
)


# ============================================================================
# REGISTRY OF ALL BUILTIN TEMPLATES
# ============================================================================

BUILTIN_TEMPLATES: dict[str, BuiltinTemplate] = {
    t.key: t for t in [
        MEETING_CONFIRMATION,
        QUICK_INVITE,
        FOLLOW_UP_BASIC,
        FOLLOW_UP_WITH_SUMMARY,
        SEND_INFORMATION,
    ]
}


def get_builtin_template(key: str) -> BuiltinTemplate | None:
    """
    Get a built-in template by its key.
    
    Args:
        key: Template key (e.g., 'meeting_confirmation')
        
    Returns:
        BuiltinTemplate or None if not found
    """
    return BUILTIN_TEMPLATES.get(key)


def list_builtin_templates(category: str | None = None) -> list[BuiltinTemplate]:
    """
    List all available built-in templates.
    
    Args:
        category: Optional filter by category
        
    Returns:
        List of BuiltinTemplate objects
    """
    templates = list(BUILTIN_TEMPLATES.values())
    if category:
        templates = [t for t in templates if t.category == category]
    return templates


def render_builtin_template(
    key: str,
    placeholders: dict[str, str],
    use_html: bool = False,
) -> tuple[str, str] | None:
    """
    Render a built-in template with provided placeholders.
    
    Args:
        key: Template key
        placeholders: Dictionary of placeholder values
        use_html: Whether to return HTML body (if available)
        
    Returns:
        Tuple of (subject, body) or None if template not found
    """
    import re
    
    template = get_builtin_template(key)
    if not template:
        return None
    
    def _render(template_str: str) -> str:
        result = template_str
        pattern = r"\{\{(\w+)\}\}"
        found = set(re.findall(pattern, template_str))
        for name in found:
            if name in placeholders:
                result = result.replace(f"{{{{{name}}}}}", str(placeholders[name]))
        # Handle conditional blocks (simple version)
        # Remove unfilled conditionals
        result = re.sub(r"\{\{#if \w+\}\}.*?\{\{/if\}\}", "", result, flags=re.DOTALL)
        return result
    
    subject = _render(template.subject_template)
    
    if use_html and template.html_body_template:
        body = _render(template.html_body_template)
    else:
        body = _render(template.text_body_template)
    
    return subject, body


def get_template_info_for_agent() -> str:
    """
    Get a formatted string describing available templates for the agent's context.
    
    Returns:
        Markdown-formatted template descriptions
    """
    lines = ["## Available Email Templates\n"]
    
    for template in BUILTIN_TEMPLATES.values():
        lines.append(f"### `{template.key}` - {template.name}")
        lines.append(f"*Category: {template.category}*")
        lines.append(f"{template.description}")
        lines.append(f"Required placeholders: {', '.join(template.placeholders)}")
        lines.append("")
    
    lines.append("### `custom` - Custom Email")
    lines.append("*Use when no template fits*")
    lines.append("Compose the email freely with full subject and body.")
    lines.append("")
    
    return "\n".join(lines)


# =============================================================================
# DB-BASED TEMPLATE TOOLS (Phase 18)
# Generic template tools that resolve based on tenant_id
# =============================================================================

import logging
from db.schema_constants import COMMUNICATION_TEMPLATES_FULL
_logger = logging.getLogger(__name__)


async def get_template_from_db(tenant_id: str, template_key: str) -> dict | None:
    """
    Get a template from DB by tenant_id and key.
    
    Phase 18: Queries communication_templates.
    
    Returns:
        Dict with template data or None if not found
    """
    try:
        from db.storage import CallStorage
        
        storage = CallStorage()
        with storage._get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT template_key, name, subject_template, content, 
                           html_content, placeholders, description, category
                    FROM {COMMUNICATION_TEMPLATES_FULL}
                    WHERE tenant_id = %s AND template_key = %s AND is_active = true
                """, (tenant_id, template_key))
                row = cur.fetchone()
                
        if row:
            return {
                "key": row[0],
                "name": row[1],
                "subject_template": row[2],
                "content": row[3],
                "html_content": row[4],
                "placeholders": row[5] or [],
                "description": row[6],
                "category": row[7],
            }
        return None
    except Exception as exc:
        _logger.error(f"Failed to get template {template_key} for tenant {tenant_id}: {exc}")
        return None


async def list_templates_from_db(tenant_id: str, category: str | None = None) -> list[dict]:
    """
    List all templates from DB for a tenant.
    
    Phase 18: Queries communication_templates.
    
    Returns:
        List of template dicts
    """
    try:
        from db.storage import CallStorage
        
        storage = CallStorage()
        with storage._get_connection() as conn:
            with conn.cursor() as cur:
                if category:
                    cur.execute("""
                        SELECT template_key, name, description, category, placeholders
                        FROM {COMMUNICATION_TEMPLATES_FULL}
                        WHERE tenant_id = %s AND is_active = true AND category = %s
                        ORDER BY category, template_key
                    """, (tenant_id, category))
                else:
                    cur.execute("""
                        SELECT template_key, name, description, category, placeholders
                        FROM {COMMUNICATION_TEMPLATES_FULL}
                        WHERE tenant_id = %s AND is_active = true
                        ORDER BY category, template_key
                    """, (tenant_id,))
                rows = cur.fetchall()
        
        return [
            {
                "key": row[0],
                "name": row[1],
                "description": row[2],
                "category": row[3],
                "placeholders": row[4] or [],
            }
            for row in rows
        ]
    except Exception as exc:
        _logger.error(f"Failed to list templates for tenant {tenant_id}: {exc}")
        return []


def render_template(template: dict, placeholders: dict[str, str], use_html: bool = False) -> tuple[str, str]:
    """
    Render a template with placeholder values.
    
    Args:
        template: Template dict from DB
        placeholders: Values for placeholders
        use_html: Use HTML content if available
        
    Returns:
        Tuple of (subject, body)
    """
    import re
    
    def _render(text: str) -> str:
        if not text:
            return ""
        result = text
        # Handle both {PLACEHOLDER} and {{placeholder}} formats
        for key, value in placeholders.items():
            result = result.replace(f"{{{key}}}", str(value))
            result = result.replace(f"{{{{{key}}}}}", str(value))
            result = result.replace(f"{{{key.upper()}}}", str(value))
        return result
    
    subject = _render(template.get("subject_template", ""))
    
    if use_html and template.get("html_content"):
        body = _render(template["html_content"])
    else:
        body = _render(template.get("content", ""))
    
    return subject, body


def create_email_template_tools(
    tenant_id: str,
    user_id: str | None = None,
    email_provider: str = "google",  # "google" or "microsoft"
):
    """
    Create email template tools for agent attachment.
    
    Phase 18: Returns callable tools for send_template_email and list_templates.
    These are generic tools that work with any tenant's templates.
    
    Args:
        tenant_id: Tenant UUID for template resolution
        user_id: User ID for OAuth email sending
        
    Returns:
        List of tool functions
    """
    @function_tool
    async def send_template_email(
        template_key: str,
        to: list[str],
        recipient_name: str,
        placeholders: dict[str, str] | None = None,
    ) -> str:
        """
        Send an email using a template from the tenant's template library.
        
        Args:
            template_key: Key of the template to use (e.g., "TC01A")
            to: List of recipient email addresses
            recipient_name: Name of the recipient (REQUIRED - must be real name)
            placeholders: Additional placeholder values
            
        Returns:
            Success message or error
        """
        # Get template from DB
        template = await get_template_from_db(tenant_id, template_key)
        if not template:
            # Try builtin templates as fallback
            builtin = get_builtin_template(template_key)
            if builtin:
                template = {
                    "key": builtin.key,
                    "name": builtin.name,
                    "subject_template": builtin.subject_template,
                    "content": builtin.text_body_template,
                    "html_content": builtin.html_body_template,
                    "placeholders": list(builtin.placeholders),
                }
            else:
                available = await list_templates_from_db(tenant_id)
                keys = [t["key"] for t in available[:10]]
                return f"Template '{template_key}' not found. Available: {', '.join(keys)}"
        
        # Validate recipient name
        if not recipient_name or recipient_name.lower() in ["student_name", "{student_name}", "name"]:
            return "Error: You must provide the recipient's ACTUAL NAME, not a placeholder."
        
        # Build placeholder values
        placeholder_values = placeholders or {}
        placeholder_values["STUDENT_NAME"] = recipient_name
        placeholder_values["recipient_name"] = recipient_name
        
        # Render template
        subject, body = render_template(template, placeholder_values, use_html=True)
        
        if not subject or not body:
            return f"Error: Template '{template_key}' could not be rendered."
        
        # Send email via user's OAuth (Google or Microsoft)
        try:
            if email_provider == "microsoft":
                from utils.microsoft_credentials import MicrosoftCredentialResolver, MicrosoftCredentialError
                from tools.microsoft_outlook_tool import MicrosoftOutlookTool, EmailRecipient
                
                try:
                    resolver = MicrosoftCredentialResolver()
                    access_token = await resolver.load_access_token(user_id)
                    
                    outlook_tool = MicrosoftOutlookTool(access_token)
                    recipients = [EmailRecipient(email=addr, name=recipient_name) for addr in to]
                    
                    result = await outlook_tool.send_email(
                        to_recipients=recipients,
                        subject=subject,
                        body_content=body,
                        content_type="HTML" if "<html>" in body.lower() else "Text",
                    )
                    return f"Email sent via Microsoft using template '{template_key}' ({template['name']}) to {recipient_name}."
                except MicrosoftCredentialError as e:
                    _logger.error(f"Microsoft OAuth error: {e}")
                    return f"Error: Microsoft not authorized for this user. {e}"
            else:
                # Default: Google OAuth
                from tools.gmail_email_tool import send_email_oauth
                
                result = await send_email_oauth(
                    user_id=user_id,
                    to=to,
                    subject=subject,
                    html_body=body,
                )
                return f"Email sent using template '{template_key}' ({template['name']}) to {recipient_name}. {result}"
        except Exception as exc:
            _logger.error(f"Failed to send template email: {exc}")
            return f"Error sending email: {exc}"
    
    @function_tool
    async def list_templates(category: str | None = None) -> str:
        """
        List available email templates for this tenant.
        
        Args:
            category: Optional category filter (e.g., "non_responsive", "follow_up")
            
        Returns:
            Formatted list of available templates
        """
        templates = await list_templates_from_db(tenant_id, category)
        
        if not templates:
            return "No templates available for this tenant."
        
        lines = ["## Available Email Templates\n"]
        
        # Group by category
        categories: dict[str, list] = {}
        for t in templates:
            cat = t.get("category", "general")
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(t)
        
        for cat, tmpl_list in categories.items():
            lines.append(f"### {cat.upper().replace('_', ' ')}")
            for t in tmpl_list:
                placeholders_str = ", ".join(t.get("placeholders", ["recipient_name"]))
                lines.append(f"- **{t['key']}**: {t.get('description', t['name'])} (placeholders: {placeholders_str})")
            lines.append("")
        
        return "\n".join(lines)
    
    return [send_template_email, list_templates]


def create_microsoft_email_template_tools(tenant_id: str, user_id: str | None = None):
    """
    Create Microsoft email template tools for agent attachment.
    
    Uses Microsoft OAuth (Outlook) instead of Google to send emails.
    
    Args:
        tenant_id: Tenant UUID for template resolution
        user_id: User ID for Microsoft OAuth token resolution
        
    Returns:
        List of tool functions [send_template_email_ms, list_templates]
    """
    @function_tool
    async def send_template_email_ms(
        template_key: str,
        to: list[str],
        recipient_name: str,
        placeholders: dict[str, str] | None = None,
    ) -> str:
        """
        Send an email using a template from the tenant's template library via Microsoft.
        
        Args:
            template_key: Key of the template to use (e.g., "TC01A")
            to: List of recipient email addresses
            recipient_name: Name of the recipient (REQUIRED - must be real name)
            placeholders: Additional placeholder values
            
        Returns:
            Success message or error
        """
        # Get template from DB
        template = await get_template_from_db(tenant_id, template_key)
        if not template:
            # Try builtin templates as fallback
            builtin = get_builtin_template(template_key)
            if builtin:
                template = {
                    "key": builtin.key,
                    "name": builtin.name,
                    "subject_template": builtin.subject_template,
                    "content": builtin.text_body_template,
                    "html_content": builtin.html_body_template,
                    "placeholders": list(builtin.placeholders),
                }
            else:
                available = await list_templates_from_db(tenant_id)
                keys = [t["key"] for t in available[:10]]
                return f"Template '{template_key}' not found. Available: {', '.join(keys)}"
        
        # Validate recipient name
        if not recipient_name or recipient_name.lower() in ["student_name", "{student_name}", "name"]:
            return "Error: You must provide the recipient's ACTUAL NAME, not a placeholder."
        
        # Build placeholder values
        placeholder_values = placeholders or {}
        placeholder_values["STUDENT_NAME"] = recipient_name
        placeholder_values["recipient_name"] = recipient_name
        
        # Render template
        subject, body = render_template(template, placeholder_values, use_html=True)
        
        if not subject or not body:
            return f"Error: Template '{template_key}' could not be rendered."
        
        # Send email via Microsoft OAuth
        try:
            from utils.microsoft_credentials import MicrosoftCredentialResolver, MicrosoftCredentialError
            from tools.microsoft_outlook_tool import MicrosoftOutlookTool, EmailRecipient
            
            resolver = MicrosoftCredentialResolver()
            access_token = await resolver.load_access_token(user_id)
            
            outlook_tool = MicrosoftOutlookTool(access_token)
            recipients = [EmailRecipient(email=addr, name=recipient_name) for addr in to]
            
            result = await outlook_tool.send_email(
                to_recipients=recipients,
                subject=subject,
                body_content=body,
                content_type="HTML" if "<html>" in body.lower() else "Text",
            )
            return f"Email sent via Microsoft using template '{template_key}' ({template['name']}) to {recipient_name}."
        except MicrosoftCredentialError as e:
            _logger.error(f"Microsoft OAuth error: {e}")
            return f"Error: Microsoft not authorized for this user. {e}"
        except Exception as exc:
            _logger.error(f"Failed to send Microsoft template email: {exc}")
            return f"Error sending email: {exc}"
    
    @function_tool
    async def list_templates(category: str | None = None) -> str:
        """
        List available email templates for this tenant.
        
        Args:
            category: Optional category filter (e.g., "non_responsive", "follow_up")
            
        Returns:
            Formatted list of available templates
        """
        templates = await list_templates_from_db(tenant_id, category)
        
        if not templates:
            return "No templates available for this tenant."
        
        lines = ["## Available Email Templates\n"]
        
        # Group by category
        categories: dict[str, list] = {}
        for t in templates:
            cat = t.get("category", "general")
            if cat not in categories:
                categories[cat] = []
            categories[cat].append(t)
        
        for cat, tmpl_list in categories.items():
            lines.append(f"### {cat.upper().replace('_', ' ')}")
            for t in tmpl_list:
                placeholders_str = ", ".join(t.get("placeholders", ["recipient_name"]))
                lines.append(f"- **{t['key']}**: {t.get('description', t['name'])} (placeholders: {placeholders_str})")
            lines.append("")
        
        return "\n".join(lines)
    
    return [send_template_email_ms, list_templates]


__all__ = [
    # Builtin templates
    "BuiltinTemplate",
    "BUILTIN_TEMPLATES",
    "get_builtin_template",
    "list_builtin_templates",
    "render_builtin_template",
    "get_template_info_for_agent",
    # Individual templates for direct import
    "MEETING_CONFIRMATION",
    "QUICK_INVITE",
    "FOLLOW_UP_BASIC",
    "FOLLOW_UP_WITH_SUMMARY",
    "SEND_INFORMATION",
    # Phase 18: DB-based template tools
    "create_email_template_tools",
    "create_microsoft_email_template_tools",
    "get_template_from_db",
    "list_templates_from_db",
    "render_template",
]

