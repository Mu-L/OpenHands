from pathlib import Path

from prompt_toolkit import PromptSession, print_formatted_text
from prompt_toolkit.completion import FuzzyWordCompleter
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.shortcuts import print_container
from prompt_toolkit.widgets import Frame, TextArea
from pydantic import SecretStr

from openhands.cli.tui import (
    COLOR_GREY,
    UserCancelledError,
    cli_confirm,
    kb_cancel,
)
from openhands.cli.utils import (
    VERIFIED_ANTHROPIC_MODELS,
    VERIFIED_MISTRAL_MODELS,
    VERIFIED_OPENAI_MODELS,
    VERIFIED_OPENHANDS_MODELS,
    VERIFIED_PROVIDERS,
    organize_models_and_providers,
)
from openhands.controller.agent import Agent
from openhands.core.config import OpenHandsConfig
from openhands.core.config.condenser_config import (
    CondenserPipelineConfig,
    ConversationWindowCondenserConfig,
)
from openhands.core.config.utils import OH_DEFAULT_AGENT
from openhands.memory.condenser.impl.llm_summarizing_condenser import (
    LLMSummarizingCondenserConfig,
)
from openhands.storage.data_models.settings import Settings
from openhands.storage.settings.file_settings_store import FileSettingsStore
from openhands.utils.llm import get_supported_llm_models


def display_settings(config: OpenHandsConfig) -> None:
    llm_config = config.get_llm_config()
    advanced_llm_settings = True if llm_config.base_url else False

    # Prepare labels and values based on settings
    labels_and_values = []
    if not advanced_llm_settings:
        # Attempt to determine provider, fallback if not directly available
        provider = getattr(
            llm_config,
            'provider',
            llm_config.model.split('/')[0] if '/' in llm_config.model else 'Unknown',
        )
        labels_and_values.extend(
            [
                ('   LLM Provider', str(provider)),
                ('   LLM Model', str(llm_config.model)),
                ('   API Key', '********' if llm_config.api_key else 'Not Set'),
            ]
        )
    else:
        labels_and_values.extend(
            [
                ('   Custom Model', str(llm_config.model)),
                ('   Base URL', str(llm_config.base_url)),
                ('   API Key', '********' if llm_config.api_key else 'Not Set'),
            ]
        )

    # Common settings
    labels_and_values.extend(
        [
            ('   Agent', str(config.default_agent)),
            (
                '   Confirmation Mode',
                'Enabled' if config.security.confirmation_mode else 'Disabled',
            ),
            (
                '   Memory Condensation',
                'Enabled' if config.enable_default_condenser else 'Disabled',
            ),
            (
                '   Search API Key',
                '********' if config.search_api_key else 'Not Set',
            ),
            (
                '   Configuration File',
                str(Path(config.file_store_path) / 'settings.json'),
            ),
        ]
    )

    # Calculate max widths for alignment
    # Ensure values are strings for len() calculation
    str_labels_and_values = [(label, str(value)) for label, value in labels_and_values]
    max_label_width = (
        max(len(label) for label, _ in str_labels_and_values)
        if str_labels_and_values
        else 0
    )

    # Construct the summary text with aligned columns
    settings_lines = [
        f'{label + ":":<{max_label_width + 1}} {value:<}'  # Changed value alignment to left (<)
        for label, value in str_labels_and_values
    ]
    settings_text = '\n'.join(settings_lines)

    container = Frame(
        TextArea(
            text=settings_text,
            read_only=True,
            style=COLOR_GREY,
            wrap_lines=True,
        ),
        title='Settings',
        style=f'fg:{COLOR_GREY}',
    )

    print_container(container)


async def get_validated_input(
    session: PromptSession,
    prompt_text: str,
    completer=None,
    validator=None,
    error_message: str = 'Input cannot be empty',
) -> str:
    session.completer = completer
    value = None

    while True:
        value = await session.prompt_async(prompt_text)

        if validator:
            is_valid = validator(value)
            if not is_valid:
                print_formatted_text('')
                print_formatted_text(HTML(f'<grey>{error_message}: {value}</grey>'))
                print_formatted_text('')
                continue
        elif not value:
            print_formatted_text('')
            print_formatted_text(HTML(f'<grey>{error_message}</grey>'))
            print_formatted_text('')
            continue

        break

    return value


def save_settings_confirmation(config: OpenHandsConfig) -> bool:
    return (
        cli_confirm(
            config,
            '\nSave new settings? (They will take effect after restart)',
            ['Yes, save', 'No, discard'],
        )
        == 0
    )


async def modify_llm_settings_basic(
    config: OpenHandsConfig, settings_store: FileSettingsStore
) -> None:
    model_list = get_supported_llm_models(config)
    organized_models = organize_models_and_providers(model_list)

    provider_list = list(organized_models.keys())
    verified_providers = [p for p in VERIFIED_PROVIDERS if p in provider_list]
    provider_list = [p for p in provider_list if p not in verified_providers]
    provider_list = verified_providers + provider_list

    provider_completer = FuzzyWordCompleter(provider_list)
    session = PromptSession(key_bindings=kb_cancel())

    # Set default provider - prefer 'anthropic' if available, otherwise use first
    provider = 'anthropic' if 'anthropic' in provider_list else provider_list[0]
    model = None
    api_key = None

    try:
        # Show the default provider but allow changing it
        print_formatted_text(
            HTML(f'\n<grey>Default provider: </grey><green>{provider}</green>')
        )

        # Show verified providers plus "Select another provider" option
        provider_choices = verified_providers + ['Select another provider']
        provider_choice = cli_confirm(
            config,
            '(Step 1/3) Select LLM Provider:',
            provider_choices,
        )

        # Ensure provider_choice is an integer (for test compatibility)
        try:
            choice_index = int(provider_choice)
        except (TypeError, ValueError):
            # If conversion fails (e.g., in tests with mocks), default to 0
            choice_index = 0

        if choice_index < len(verified_providers):
            # User selected one of the verified providers
            provider = verified_providers[choice_index]
        else:
            # User selected "Select another provider" - use manual selection
            # Define a validator function that prints an error message
            def provider_validator(x):
                is_valid = x in organized_models
                if not is_valid:
                    print_formatted_text(
                        HTML('<grey>Invalid provider selected: {}</grey>'.format(x))
                    )
                return is_valid

            provider = await get_validated_input(
                session,
                '(Step 1/3) Select LLM Provider (TAB for options, CTRL-c to cancel): ',
                completer=provider_completer,
                validator=provider_validator,
                error_message='Invalid provider selected',
            )

        # Make sure the provider exists in organized_models
        if provider not in organized_models:
            # If the provider doesn't exist, prefer 'anthropic' if available,
            # otherwise use the first provider
            provider = (
                'anthropic'
                if 'anthropic' in organized_models
                else next(iter(organized_models.keys()))
            )

        provider_models = organized_models[provider]['models']
        if provider == 'openai':
            provider_models = [
                m for m in provider_models if m not in VERIFIED_OPENAI_MODELS
            ]
            provider_models = VERIFIED_OPENAI_MODELS + provider_models
        if provider == 'anthropic':
            provider_models = [
                m for m in provider_models if m not in VERIFIED_ANTHROPIC_MODELS
            ]
            provider_models = VERIFIED_ANTHROPIC_MODELS + provider_models
        if provider == 'mistral':
            provider_models = [
                m for m in provider_models if m not in VERIFIED_MISTRAL_MODELS
            ]
            provider_models = VERIFIED_MISTRAL_MODELS + provider_models
        if provider == 'openhands':
            provider_models = [
                m for m in provider_models if m not in VERIFIED_OPENHANDS_MODELS
            ]
            provider_models = VERIFIED_OPENHANDS_MODELS + provider_models

        # Set default model to the best verified model for the provider
        if provider == 'anthropic' and VERIFIED_ANTHROPIC_MODELS:
            # Use the first model in the VERIFIED_ANTHROPIC_MODELS list as it's the best/newest
            default_model = VERIFIED_ANTHROPIC_MODELS[0]
        elif provider == 'openai' and VERIFIED_OPENAI_MODELS:
            # Use the first model in the VERIFIED_OPENAI_MODELS list as it's the best/newest
            default_model = VERIFIED_OPENAI_MODELS[0]
        elif provider == 'mistral' and VERIFIED_MISTRAL_MODELS:
            # Use the first model in the VERIFIED_MISTRAL_MODELS list as it's the best/newest
            default_model = VERIFIED_MISTRAL_MODELS[0]
        elif provider == 'openhands' and VERIFIED_OPENHANDS_MODELS:
            # Use the first model in the VERIFIED_OPENHANDS_MODELS list as it's the best/newest
            default_model = VERIFIED_OPENHANDS_MODELS[0]
        else:
            # For other providers, use the first model in the list
            default_model = (
                provider_models[0] if provider_models else 'claude-sonnet-4-20250514'
            )

        # For OpenHands provider, directly show all verified models without the "use default" option
        if provider == 'openhands':
            # Create a list of models for the cli_confirm function
            model_choices = VERIFIED_OPENHANDS_MODELS

            model_choice = cli_confirm(
                config,
                (
                    '(Step 2/3) Select Available OpenHands Model:\n'
                    + 'LLM usage is billed at the providers’ rates with no markup. Details: https://docs.all-hands.dev/usage/llms/openhands-llms'
                ),
                model_choices,
            )

            # Get the selected model from the list
            model = model_choices[model_choice]

        else:
            # For other providers, show the default model but allow changing it
            print_formatted_text(
                HTML(f'\n<grey>Default model: </grey><green>{default_model}</green>')
            )
            change_model = (
                cli_confirm(
                    config,
                    'Do you want to use a different model?',
                    [f'Use {default_model}', 'Select another model'],
                )
                == 1
            )

            if change_model:
                model_completer = FuzzyWordCompleter(provider_models)

                # Define a validator function that allows custom models but shows a warning
                def model_validator(x):
                    # Allow any non-empty model name
                    if not x.strip():
                        return False

                    # Show a warning for models not in the predefined list, but still allow them
                    if x not in provider_models:
                        print_formatted_text(
                            HTML(
                                f'<yellow>Warning: {x} is not in the predefined list for provider {provider}. '
                                f'Make sure this model name is correct.</yellow>'
                            )
                        )
                    return True

                model = await get_validated_input(
                    session,
                    '(Step 2/3) Select LLM Model (TAB for options, CTRL-c to cancel): ',
                    completer=model_completer,
                    validator=model_validator,
                    error_message='Model name cannot be empty',
                )
            else:
                # Use the default model
                model = default_model

        if provider == 'openhands':
            print_formatted_text(
                HTML(
                    '\nYou can find your OpenHands LLM API Key in the <a href="https://app.all-hands.dev/settings/api-keys">API Keys</a> tab of OpenHands Cloud: https://app.all-hands.dev/settings/api-keys'
                )
            )

        api_key = await get_validated_input(
            session,
            '(Step 3/3) Enter API Key (CTRL-c to cancel): ',
            error_message='API Key cannot be empty',
        )

    except (
        UserCancelledError,
        KeyboardInterrupt,
        EOFError,
    ):
        return  # Return on exception

    # The try-except block above ensures we either have valid inputs or we've already returned
    # No need to check for None values here

    save_settings = save_settings_confirmation(config)

    if not save_settings:
        return

    llm_config = config.get_llm_config()
    llm_config.model = f'{provider}{organized_models[provider]["separator"]}{model}'
    llm_config.api_key = SecretStr(api_key)
    llm_config.base_url = None
    config.set_llm_config(llm_config)

    config.default_agent = OH_DEFAULT_AGENT
    config.enable_default_condenser = True

    agent_config = config.get_agent_config(config.default_agent)
    agent_config.condenser = LLMSummarizingCondenserConfig(
        llm_config=llm_config,
        type='llm',
    )
    config.set_agent_config(agent_config, config.default_agent)

    settings = await settings_store.load()
    if not settings:
        settings = Settings()

    settings.llm_model = f'{provider}{organized_models[provider]["separator"]}{model}'
    settings.llm_api_key = SecretStr(api_key)
    settings.llm_base_url = None
    settings.agent = OH_DEFAULT_AGENT
    settings.enable_default_condenser = True

    await settings_store.store(settings)


async def modify_llm_settings_advanced(
    config: OpenHandsConfig, settings_store: FileSettingsStore
) -> None:
    session = PromptSession(key_bindings=kb_cancel())

    custom_model = None
    base_url = None
    api_key = None
    agent = None

    try:
        custom_model = await get_validated_input(
            session,
            '(Step 1/6) Custom Model (CTRL-c to cancel): ',
            error_message='Custom Model cannot be empty',
        )

        base_url = await get_validated_input(
            session,
            '(Step 2/6) Base URL (CTRL-c to cancel): ',
            error_message='Base URL cannot be empty',
        )

        api_key = await get_validated_input(
            session,
            '(Step 3/6) API Key (CTRL-c to cancel): ',
            error_message='API Key cannot be empty',
        )

        agent_list = Agent.list_agents()
        agent_completer = FuzzyWordCompleter(agent_list)
        agent = await get_validated_input(
            session,
            '(Step 4/6) Agent (TAB for options, CTRL-c to cancel): ',
            completer=agent_completer,
            validator=lambda x: x in agent_list,
            error_message='Invalid agent selected',
        )

        enable_confirmation_mode = (
            cli_confirm(
                config,
                question='(Step 5/6) Confirmation Mode (CTRL-c to cancel):',
                choices=['Enable', 'Disable'],
            )
            == 0
        )

        enable_memory_condensation = (
            cli_confirm(
                config,
                question='(Step 6/6) Memory Condensation (CTRL-c to cancel):',
                choices=['Enable', 'Disable'],
            )
            == 0
        )

    except (
        UserCancelledError,
        KeyboardInterrupt,
        EOFError,
    ):
        return  # Return on exception

    # The try-except block above ensures we either have valid inputs or we've already returned
    # No need to check for None values here

    save_settings = save_settings_confirmation(config)

    if not save_settings:
        return

    llm_config = config.get_llm_config()
    llm_config.model = custom_model
    llm_config.base_url = base_url
    llm_config.api_key = SecretStr(api_key)
    config.set_llm_config(llm_config)

    config.default_agent = agent

    config.security.confirmation_mode = enable_confirmation_mode

    agent_config = config.get_agent_config(config.default_agent)
    if enable_memory_condensation:
        agent_config.condenser = CondenserPipelineConfig(
            type='pipeline',
            condensers=[
                ConversationWindowCondenserConfig(type='conversation_window'),
                # Use LLMSummarizingCondenserConfig with the custom llm_config
                LLMSummarizingCondenserConfig(
                    llm_config=llm_config, type='llm', keep_first=4, max_size=120
                ),
            ],
        )

    else:
        agent_config.condenser = ConversationWindowCondenserConfig(
            type='conversation_window'
        )
    config.set_agent_config(agent_config)

    settings = await settings_store.load()
    if not settings:
        settings = Settings()

    settings.llm_model = custom_model
    settings.llm_api_key = SecretStr(api_key)
    settings.llm_base_url = base_url
    settings.agent = agent
    settings.confirmation_mode = enable_confirmation_mode
    settings.enable_default_condenser = enable_memory_condensation

    await settings_store.store(settings)


async def modify_search_api_settings(
    config: OpenHandsConfig, settings_store: FileSettingsStore
) -> None:
    """Modify search API settings."""
    session = PromptSession(key_bindings=kb_cancel())

    search_api_key = None

    try:
        print_formatted_text(
            HTML(
                '\n<grey>Configure Search API Key for enhanced search capabilities.</grey>'
            )
        )
        print_formatted_text(
            HTML('<grey>You can get a Tavily API key from: https://tavily.com/</grey>')
        )
        print_formatted_text('')

        # Show current status
        current_key_status = '********' if config.search_api_key else 'Not Set'
        print_formatted_text(
            HTML(
                f'<grey>Current Search API Key: </grey><green>{current_key_status}</green>'
            )
        )
        print_formatted_text('')

        # Ask if user wants to modify
        modify_key = cli_confirm(
            config,
            'Do you want to modify the Search API Key?',
            ['Set/Update API Key', 'Remove API Key', 'Keep current setting'],
        )

        if modify_key == 0:  # Set/Update API Key
            search_api_key = await get_validated_input(
                session,
                'Enter Tavily Search API Key. You can get it from https://www.tavily.com/ (starts with tvly-, CTRL-c to cancel): ',
                validator=lambda x: x.startswith('tvly-') if x.strip() else False,
                error_message='Search API Key must start with "tvly-"',
            )
        elif modify_key == 1:  # Remove API Key
            search_api_key = ''  # Empty string to remove the key
        else:  # Keep current setting
            return

    except (
        UserCancelledError,
        KeyboardInterrupt,
        EOFError,
    ):
        return  # Return on exception

    save_settings = save_settings_confirmation(config)

    if not save_settings:
        return

    # Update config
    config.search_api_key = SecretStr(search_api_key) if search_api_key else None

    # Update settings store
    settings = await settings_store.load()
    if not settings:
        settings = Settings()

    settings.search_api_key = SecretStr(search_api_key) if search_api_key else None

    await settings_store.store(settings)
