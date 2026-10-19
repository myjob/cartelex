cd /tmp/docs
for dir in */; do
    if [ -d "${dir}${dir}" ]; then
        echo "Moving content from ${dir}${dir} to $dir"
        # echo mv "${dir}${dir}"* "$dir"
    fi
done
