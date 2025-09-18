
# >>> mamba initialize >>>
# !! Contents within this block are managed by 'mamba init' !!
export MAMBA_EXE="/Users/shainov07/.micromamba/bin/micromamba";
export MAMBA_ROOT_PREFIX="/Users/shainov07/micromamba";
__mamba_setup="$("$MAMBA_EXE" shell hook --shell zsh --prefix "$MAMBA_ROOT_PREFIX" 2> /dev/null)"
if [ $? -eq 0 ]; then
    eval "$__mamba_setup"
else
    if [ -f "/Users/shainov07/micromamba/etc/profile.d/micromamba.sh" ]; then
        . "/Users/shainov07/micromamba/etc/profile.d/micromamba.sh"
    else
        export  PATH="/Users/shainov07/micromamba/bin:$PATH"  # extra space after export prevents interference from conda init
    fi
fi
unset __mamba_setup
# <<< mamba initialize <<<

# >>> conda initialize >>>
# !! Contents within this block are managed by 'conda init' !!
__conda_setup="$('/Users/shainov07/micromamba/bin/conda' 'shell.zsh' 'hook' 2> /dev/null)"
if [ $? -eq 0 ]; then
    eval "$__conda_setup"
else
    if [ -f "/Users/shainov07/micromamba/etc/profile.d/conda.sh" ]; then
        . "/Users/shainov07/micromamba/etc/profile.d/conda.sh"
    else
        export PATH="/Users/shainov07/micromamba/bin:$PATH"
    fi
fi
unset __conda_setup
# <<< conda initialize <<<
export FLYCTL_INSTALL="$HOME/.fly"
export PATH="$FLYCTL_INSTALL/bin:$PATH"
